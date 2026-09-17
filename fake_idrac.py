#!/usr/bin/env python3
"""Small in-memory HTTP fake for testing iDRAC fan clients."""

import argparse
import json
import threading
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class FanState:
    def __init__(self, count=6, pwm=30):
        self._lock = threading.Lock()
        self.manual = False
        self.pci_fan_response = True
        self.fan_offset = None
        self._default = {"pwm": pwm, "rpm": pwm * 100, "status": "ok"}
        self.fans = {str(i): {"id": i, **self._default,
                              "thresholds": [0, 10, 20, 80, 90, 100]}
                     for i in range(1, count + 1)}

    def snapshot(self):
        with self._lock:
            return {k: dict(v) for k, v in self.fans.items()}

    def sdr_records(self):
        records = []
        for index, fan in enumerate(self.fans.values(), 1):
            name = f"Fan{index}".encode()
            body = bytearray((0x20, 0, 0x4f + index, 0x03, index, 0xff, 0xc0, 0x04, 1))
            body.extend(bytes(6))
            body.extend((0, 0x12, 0))  # unsigned RPM
            body.extend((0, 100, 0, 0, 0, 0, 0, 7, 30, 100, 0, 100, 0))
            body.extend((100, 90, 80, 20, 10, 0, 1, 1, 0, 0, 0))
            body.extend((0xc0 | len(name), *name))
            body.extend(bytes(16 - len(name)))
            records.append((index - 1).to_bytes(2, "little") + bytes((0x51, 1, len(body))) + body)
        return records

    def set_pwm(self, fan_id, pwm):
        if not isinstance(pwm, int) or isinstance(pwm, bool) or not 0 <= pwm <= 100:
            raise ValueError("pwm must be an integer from 0 to 100")
        with self._lock:
            if fan_id not in self.fans:
                raise KeyError(fan_id)
            self.fans[fan_id].update(pwm=pwm, rpm=pwm * 100)
            print(f"fan {fan_id}: PWM={pwm}% RPM={pwm * 100}", flush=True)

    def set_thresholds(self, fan_id, values):
        with self._lock:
            if fan_id not in self.fans:
                raise KeyError(fan_id)
            self.fans[fan_id]["thresholds"] = list(values)
            print(f"fan {fan_id}: thresholds={list(values)}", flush=True)


class IPMIUDPHandler(socketserver.BaseRequestHandler):
    """Minimal IPMI v1.5/RMCP handler for ipmitool -I lan."""
    state = None
    reservation = 0x42

    @staticmethod
    def checksum(data):
        return (-sum(data)) & 0xff

    def response(self, request, payload):
        # IPMI response message: responder address/netfn, checksum, requester...
        message = bytearray((0x81, request[15] | 1))
        message.append(self.checksum(message))
        message.extend((request[17], request[18], request[19]))
        message.extend(payload)
        message.append(self.checksum(message[3:]))
        packet = bytes((6, 0, 0xff, 7, 0)) + request[5:13] + bytes((len(message),)) + message
        return packet

    def handle(self):
        request, sock = self.request
        # Keep the fake quiet by default; set DEBUG_IPMI=1 when diagnosing a client.
        if request[:4] == bytes((6, 0, 0xff, 6)):
            pong = bytes.fromhex("06 00 ff 06 00 00 11 be 40 00 00 00 00 00 00 00 00 00 00 00")
            sock.sendto(pong, self.client_address)
            return
        if len(request) < 20:
            return
        message = request[14:]
        command = message[5]
        netfn = message[1] >> 2
        if command == 0x38:  # Get Channel Authentication Capabilities
            payload = bytes((0, 0x0e, 0x01, 0, 0, 0, 0, 0, 0))  # completion + none auth
        elif command == 0x39:  # Activate Session
            payload = bytes((0, 0, 4)) + (0x1234).to_bytes(4, "little") + bytes(4)
        elif netfn == 0x30 and command == 0x30:
            data = message[6:]
            if data[:2] == bytes((0x01, 0x00)):
                self.state.manual = True
                print("fan control: manual", flush=True)
            elif data[:2] == bytes((0x01, 0x01)):
                self.state.manual = False
                print("fan control: automatic", flush=True)
            elif data[:2] == bytes((0x02, 0xff)) and len(data) >= 3:
                self.state.manual = True
                pwm = min(data[2], 100)
                for fan_id in self.state.fans:
                    self.state.set_pwm(fan_id, pwm)
            payload = bytes((0,))
        elif netfn == 0x30 and command == 0xce:
            data = message[6:]
            if len(data) >= 2 and data[1] == 0x16:
                if data[0] == 0x01:
                    payload = bytes((0, int(self.state.pci_fan_response)))
                else:
                    self.state.pci_fan_response = not bool(data[8]) if len(data) > 8 else self.state.pci_fan_response
                    print(f"third-party PCI fan response: {'enabled' if self.state.pci_fan_response else 'disabled'}", flush=True)
                    payload = bytes((0,))
            elif len(data) >= 2 and data[1] == 0x09:
                self.state.fan_offset = "high" if data[-3] else "low"
                print(f"fan offset: {self.state.fan_offset}", flush=True)
                payload = bytes((0,))
        elif netfn == 0x0a and command == 0x20:  # Get SDR repository info
            count = len(self.state.fans)
            payload = bytes((0, 0x51)) + count.to_bytes(2, "little") + bytes((0xff, 0xff)) + bytes(9)
        elif netfn == 0x0a and command == 0x22:  # Reserve SDR repository
            payload = bytes((0,)) + self.reservation.to_bytes(2, "little")
        elif netfn == 0x0a and command == 0x23:  # Get SDR
            data = message[6:]
            record_id = int.from_bytes(data[2:4], "little") if len(data) >= 4 else 0
            offset = data[4] if len(data) > 4 else 0
            length = data[5] if len(data) > 5 else 0xff
            records = self.state.sdr_records()
            record = next((r for r in records if int.from_bytes(r[:2], "little") == record_id), b"")
            next_id = record_id + 1 if record_id < len(records) - 1 else 0xffff
            payload = bytes((0,)) + next_id.to_bytes(2, "little") + record[offset:offset + length]
        elif netfn == 0x04 and command == 0x2d:  # Get Sensor Reading
            sensor_num = message[6] if len(message) > 6 else 0
            fan_index = sensor_num - 0x4f
            rpm = self.state.fans.get(str(fan_index), {}).get("rpm", 0)
            # ipmitool's LAN parser expects the completion byte followed by the
            # reading-validity byte; keep the compact fake reading in range.
            payload = bytes((0, min(rpm // 100, 100), 0x40, 0))
        elif netfn == 0x04 and command == 0x27:  # Get Sensor Thresholds
            sensor_num = message[6] if len(message) > 6 else 0
            fan_id = str(sensor_num - 0x4f)
            values = self.state.fans.get(fan_id, {}).get("thresholds", [0] * 6)
            payload = bytes((0, *values))
        elif netfn == 0x04 and command == 0x26:  # Set Sensor Thresholds
            data = message[6:]
            sensor_num = data[0] if data else 0
            fan_id = str(sensor_num - 0x4f)
            if len(data) >= 8 and fan_id in self.state.fans:
                values = list(self.state.fans[fan_id]["thresholds"])
                mask = data[1]
                value = data[2]
                if mask:
                    values[(mask & -mask).bit_length() - 1] = value
                self.state.set_thresholds(fan_id, values)
                payload = bytes((0,))
            else:
                payload = bytes((0xc9,))
        else:
            # Standard discovery/session commands only need a successful response.
            payload = bytes((0,))
            if command == 0x3b:  # Get Device ID
                payload += bytes((0x20, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0))
            elif netfn == 0x2c and command in (0x00, 0x3e):
                # Tell ipmitool's optional PICMG/HPM probes that this fake has no
                # such extensions; those probes are unrelated to fan control.
                payload = bytes((0xc1,))
        sock.sendto(self.response(request, payload), self.client_address)


def serve_ipmi(host, port, state):
    IPMIUDPHandler.state = state
    server = socketserver.ThreadingUDPServer((host, port), IPMIUDPHandler)
    server.serve_forever()


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, code, value):
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts == ["fans"]:
                self.send_json(200, {"fans": list(state.snapshot().values())})
            elif parts == ["state"]:
                self.send_json(200, {"manual": state.manual, "pci_fan_response": state.pci_fan_response,
                                     "fan_offset": state.fan_offset, "fans": list(state.snapshot().values())})
            elif len(parts) == 2 and parts[0] == "fans":
                fan = state.snapshot().get(parts[1])
                self.send_json(200, fan) if fan else self.send_json(404, {"error": "fan not found"})
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if len(parts) != 2 or parts[0] != "fans":
                self.send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                state.set_pwm(parts[1], body["pwm"])
                self.send_json(200, state.snapshot()[parts[1]])
            except KeyError as exc:
                self.send_json(404 if str(exc).strip("'") == parts[1] else 400,
                               {"error": "fan not found" if str(exc).strip("'") == parts[1] else "missing pwm"})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})

        def log_message(self, *_):
            pass

    return Handler


def serve(host="127.0.0.1", port=8000, count=6, pwm=30, ipmi_port=623):
    state = FanState(count, pwm)
    threading.Thread(target=serve_ipmi, args=(host, ipmi_port, state), daemon=True).start()
    server = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"fake iDRAC listening on http://{host}:{server.server_port}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--fans", type=int, default=6)
    parser.add_argument("--pwm", type=int, default=30)
    parser.add_argument("--ipmi-port", type=int, default=623)
    args = parser.parse_args()
    serve(args.host, args.port, args.fans, args.pwm, args.ipmi_port)
