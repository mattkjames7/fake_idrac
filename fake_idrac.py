#!/usr/bin/env python3
"""Small in-memory HTTP fake for testing iDRAC fan clients."""

import argparse
import hashlib
import hmac
import json
import os
import threading
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


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
    """Minimal IPMI v1.5 and v2.0/RMCP+ handler for ipmitool."""
    state = None
    reservation = 0x42
    sessions = {}
    sessions_lock = threading.Lock()

    @staticmethod
    def checksum(data):
        return (-sum(data)) & 0xff

    def ipmi_response(self, message, payload):
        # IPMI response message: responder address/netfn, checksum, requester...
        # NetFn occupies bits 7:2.  A response uses the following (odd) NetFn;
        # bit 0 is part of the LUN and must not be used as the response flag.
        response = bytearray((0x81, message[1] | 0x04))
        response.append(self.checksum(response))
        response.extend(message[3:6])
        response.extend(payload)
        response.append(self.checksum(response[3:]))
        return bytes(response)

    def dispatch(self, message):
        """Return the IPMI response data, including its completion code."""
        command = message[5]
        netfn = message[1] >> 2
        if command == 0x38:  # Get Channel Authentication Capabilities
            # Auth type "none", IPMI 2.0 data available, a named user exists,
            # and both IPMI 1.5 and 2.0 are supported on this channel.
            return bytes((0, 0x0e, 0x81, 0x04, 0x03, 0, 0, 0, 0))
        if command == 0x39:  # Activate Session
            return bytes((0, 0, 4)) + (0x1234).to_bytes(4, "little") + bytes(4)
        if command == 0x54:  # Get Channel Cipher Suites (optional discovery)
            # Suites 3 (SHA1-96/AES) and 17 (SHA256-128/AES).  Returning
            # fewer than 16 record bytes also marks this as the final page.
            return bytes.fromhex("00 0e c0 03 01 41 81 c0 11 03 44 81")
        if netfn == 0x30 and command == 0x30:
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
            return bytes((0,))
        if netfn == 0x30 and command == 0xce:
            data = message[6:]
            if len(data) >= 2 and data[1] == 0x16:
                if data[0] == 0x01:
                    return bytes((0, int(self.state.pci_fan_response)))
                self.state.pci_fan_response = not bool(data[8]) if len(data) > 8 else self.state.pci_fan_response
                print(f"third-party PCI fan response: {'enabled' if self.state.pci_fan_response else 'disabled'}", flush=True)
                return bytes((0,))
            if len(data) >= 2 and data[1] == 0x09:
                self.state.fan_offset = "high" if data[-3] else "low"
                print(f"fan offset: {self.state.fan_offset}", flush=True)
                return bytes((0,))
        if netfn == 0x0a and command == 0x20:  # Get SDR repository info
            count = len(self.state.fans)
            return bytes((0, 0x51)) + count.to_bytes(2, "little") + bytes((0xff, 0xff)) + bytes(9)
        if netfn == 0x0a and command == 0x22:  # Reserve SDR repository
            return bytes((0,)) + self.reservation.to_bytes(2, "little")
        if netfn == 0x0a and command == 0x23:  # Get SDR
            data = message[6:]
            record_id = int.from_bytes(data[2:4], "little") if len(data) >= 4 else 0
            offset = data[4] if len(data) > 4 else 0
            length = data[5] if len(data) > 5 else 0xff
            records = self.state.sdr_records()
            record = next((r for r in records if int.from_bytes(r[:2], "little") == record_id), b"")
            next_id = record_id + 1 if record_id < len(records) - 1 else 0xffff
            return bytes((0,)) + next_id.to_bytes(2, "little") + record[offset:offset + length]
        if netfn == 0x04 and command == 0x2d:  # Get Sensor Reading
            sensor_num = message[6] if len(message) > 6 else 0
            fan_index = sensor_num - 0x4f
            rpm = self.state.fans.get(str(fan_index), {}).get("rpm", 0)
            return bytes((0, min(rpm // 100, 100), 0x40, 0))
        if netfn == 0x04 and command == 0x27:  # Get Sensor Thresholds
            sensor_num = message[6] if len(message) > 6 else 0
            fan_id = str(sensor_num - 0x4f)
            values = self.state.fans.get(fan_id, {}).get("thresholds", [0] * 6)
            return bytes((0, *values))
        if netfn == 0x04 and command == 0x26:  # Set Sensor Thresholds
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
                return bytes((0,))
            return bytes((0xc9,))

        payload = bytes((0,))
        if command == 0x3b:  # Set Session Privilege Level
            payload += bytes((message[6] & 0x0f,))
        elif command == 0x01:  # Get Device ID
            payload += bytes((0x20, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0))
        elif netfn == 0x2c and command in (0x00, 0x3e):
            payload = bytes((0xc1,))
        return payload

    @staticmethod
    def rmcpplus_packet(payload_type, payload, session_id=0, sequence=0):
        return (bytes((6, 0, 0xff, 7, 6, payload_type))
                + session_id.to_bytes(4, "little")
                + sequence.to_bytes(4, "little")
                + len(payload).to_bytes(2, "little") + payload)

    def handle_rmcpplus(self, request):
        payload_type = request[5] & 0x3f
        length = int.from_bytes(request[14:16], "little")
        payload = request[16:16 + length]

        if payload_type == 0x10:  # RMCP+ Open Session Request
            console_id = int.from_bytes(payload[4:8], "little")
            bmc_id = int.from_bytes(os.urandom(4), "little") or 1
            session = {"console_id": console_id, "bmc_id": bmc_id,
                       "auth": payload[12], "integrity": payload[20],
                       "crypt": payload[28], "sequence": 1}
            with self.sessions_lock:
                self.sessions[bmc_id] = session
            response = (bytes((payload[0], 0, 4, 0)) + payload[4:8]
                        + bmc_id.to_bytes(4, "little")
                        + bytes((0, 0, 0, 8, session["auth"], 0, 0, 0))
                        + bytes((1, 0, 0, 8, session["integrity"], 0, 0, 0))
                        + bytes((2, 0, 0, 8, session["crypt"], 0, 0, 0)))
            return self.rmcpplus_packet(0x11, response)

        if payload_type == 0x12:  # RAKP Message 1
            bmc_id = int.from_bytes(payload[4:8], "little")
            session = self.sessions.get(bmc_id)
            if not session:
                return None
            session["console_rand"] = payload[8:24]
            session["role"] = payload[24]
            username_length = payload[27]
            session["username"] = payload[28:28 + username_length]
            session["bmc_rand"] = os.urandom(16)
            session["guid"] = bytes.fromhex("00112233445566778899aabbccddeeff")
            if session["username"] != b"user":
                response = (bytes((payload[0], 0x0d, 0, 0))
                            + session["console_id"].to_bytes(4, "little"))
                return self.rmcpplus_packet(0x13, response)
            key = b"pass"
            material = (session["console_id"].to_bytes(4, "little")
                        + bmc_id.to_bytes(4, "little") + session["console_rand"]
                        + session["bmc_rand"] + session["guid"]
                        + bytes((session["role"], username_length)) + session["username"])
            digest = hashlib.sha256 if session["auth"] == 3 else hashlib.sha1
            auth_code = hmac.new(key, material, digest).digest() if session["auth"] else b""
            response = (bytes((payload[0], 0, 0, 0))
                        + session["console_id"].to_bytes(4, "little")
                        + session["bmc_rand"] + session["guid"] + auth_code)
            return self.rmcpplus_packet(0x13, response)

        if payload_type == 0x14:  # RAKP Message 3
            bmc_id = int.from_bytes(payload[4:8], "little")
            session = self.sessions.get(bmc_id)
            if not session:
                return None
            key = b"pass"
            rakp3_material = (session["bmc_rand"]
                              + session["console_id"].to_bytes(4, "little")
                              + bytes((session["role"], len(session["username"])))
                              + session["username"])
            digest = hashlib.sha256 if session["auth"] == 3 else hashlib.sha1
            auth_length = digest().digest_size
            expected = hmac.new(key, rakp3_material, digest).digest()
            if not hmac.compare_digest(expected, payload[8:8 + auth_length]):
                response = (bytes((payload[0], 0x0f, 0, 0))
                            + session["console_id"].to_bytes(4, "little"))
                return self.rmcpplus_packet(0x15, response)
            sik_material = (session["console_rand"] + session["bmc_rand"]
                            + bytes((session["role"], len(session["username"])))
                            + session["username"])
            session["sik"] = hmac.new(key, sik_material, digest).digest()
            session["k1"] = hmac.new(session["sik"], bytes((1,)) * 20, digest).digest()
            session["k2"] = hmac.new(session["sik"], bytes((2,)) * 20, digest).digest()
            rakp4_material = (session["console_rand"] + bmc_id.to_bytes(4, "little")
                              + session["guid"])
            check_length = 16 if session["integrity"] == 4 else 12
            check = hmac.new(session["sik"], rakp4_material, digest).digest()[:check_length]
            response = (bytes((payload[0], 0, 0, 0))
                        + session["console_id"].to_bytes(4, "little") + check)
            return self.rmcpplus_packet(0x15, response)

        if payload_type == 0 and not request[5] & 0xc0:
            # Cipher-suite discovery is sent in an RMCP+ envelope before a
            # session exists. A prompt error is enough for ipmitool to fall
            # back to the explicitly/default selected suite without retries.
            response = self.ipmi_response(payload, self.dispatch(payload))
            return self.rmcpplus_packet(0, response)

        if payload_type == 0 and request[5] & 0xc0:
            bmc_id = int.from_bytes(request[6:10], "little")
            session = self.sessions.get(bmc_id)
            if not session:
                return None
            digest = hashlib.sha256 if session["integrity"] == 4 else hashlib.sha1
            auth_length = 16 if session["integrity"] == 4 else 12
            expected = hmac.new(session["k1"], request[4:-auth_length], digest).digest()[:auth_length]
            if not hmac.compare_digest(expected, request[-auth_length:]):
                return None
            if request[5] & 0x80:
                iv, ciphertext = payload[:16], payload[16:]
                padded = Cipher(algorithms.AES(session["k2"][:16]), modes.CBC(iv)).decryptor().update(ciphertext)
                payload = padded[:-padded[-1] - 1]
            response = self.ipmi_response(payload, self.dispatch(payload))
            if session["crypt"]:
                pad_length = (15 - len(response)) % 16
                padded = response + bytes(range(1, pad_length + 1)) + bytes((pad_length,))
                iv = os.urandom(16)
                response = iv + Cipher(algorithms.AES(session["k2"][:16]), modes.CBC(iv)).encryptor().update(padded)
            packet = bytearray(self.rmcpplus_packet(0xc0, response, session["console_id"], session["sequence"]))
            session["sequence"] += 1
            integrity_padding = (4 - ((len(packet) - 4 + 2) % 4)) % 4
            packet.extend(bytes((0xff,)) * integrity_padding)
            packet.extend((integrity_padding, 7))
            packet.extend(hmac.new(session["k1"], packet[4:], digest).digest()[:auth_length])
            return bytes(packet)
        return None

    def handle(self):
        request, sock = self.request
        # Keep the fake quiet by default; set DEBUG_IPMI=1 when diagnosing a client.
        if request[:4] == bytes((6, 0, 0xff, 6)):
            pong = bytes.fromhex("06 00 ff 06 00 00 11 be 40 00 00 00 00 00 00 00 00 00 00 00")
            sock.sendto(pong, self.client_address)
            return
        if len(request) < 20:
            return
        if request[4] == 6:
            response = self.handle_rmcpplus(request)
            if response:
                sock.sendto(response, self.client_address)
            return
        message = request[14:]
        response = self.ipmi_response(message, self.dispatch(message))
        packet = bytes((6, 0, 0xff, 7, 0)) + request[5:13] + bytes((len(response),)) + response
        sock.sendto(packet, self.client_address)


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
