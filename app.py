"""톡톡 라이브 퀴즈: dependency-free live classroom quiz server."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_ROOM_SIZE = 80
ROOM_TTL_SECONDS = 24 * 60 * 60
ROOMS: dict[str, "Room"] = {}
ROOMS_LOCK = threading.RLock()
LOGGER = logging.getLogger("toktok")


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Player:
    token: str
    name: str
    joined: int
    score: int = 0
    round_start_score: int = 0
    answer: int | None = None
    elapsed_ms: int | None = None
    earned: int = 0
    correct_rounds: int = 0
    total_correct_ms: int = 0


@dataclass
class Room:
    code: str
    teacher_token: str
    created_at: float = field(default_factory=time.time)
    touched_at: float = field(default_factory=time.time)
    status: str = "draft"
    question: str = ""
    options: list[str] = field(default_factory=lambda: ["", "", "", ""])
    correct_index: int | None = None
    multiplier_hundredths: int = 100  # 100 = x1.00; use integer maths to avoid float rounding.
    round_no: int = 0
    draft_revision: int = -1
    started_ns: int | None = None
    started_at: int | None = None
    version: int = 0
    players: OrderedDict[str, Player] = field(default_factory=OrderedDict)
    listeners: set[threading.Event] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def touch(self):
        self.touched_at = time.time()

    def changed(self):
        self.version += 1
        self.touch()
        for listener in tuple(self.listeners):
            listener.set()

    def authenticate(self, token: str) -> tuple[str, Player | None]:
        if token and secrets.compare_digest(token, self.teacher_token):
            return "teacher", None
        player = self.players.get(token)
        if player is None:
            raise APIError(401, "입장 정보가 없거나 만료되었습니다. 다시 입장해 주세요.")
        return "student", player

    def must_be_teacher(self, token: str):
        role, _ = self.authenticate(token)
        if role != "teacher":
            raise APIError(403, "교사만 사용할 수 있는 기능입니다.")

    def must_be_student(self, token: str) -> Player:
        role, player = self.authenticate(token)
        if role != "student" or player is None:
            raise APIError(403, "학생만 답을 제출할 수 있습니다.")
        return player

    def snapshot(self, token: str) -> dict:
        role, player = self.authenticate(token)
        count = len(self.players)
        submitted = sum(p.answer is not None for p in self.players.values())
        payload = {
            "code": self.code,
            "role": role,
            "status": self.status,
            "version": self.version,
            "round_no": self.round_no,
            "question": self.question,
            "options": self.options[:],
            "multiplier": self.multiplier_hundredths / 100,
            "participants": count,
            "submitted": submitted,
            "started_at": self.started_at,
        }
        if role == "teacher":
            payload["correct_index"] = self.correct_index
            payload["draft_revision"] = self.draft_revision
            payload["players"] = [
                {
                    "name": p.name,
                    "score": p.score,
                    "submitted": p.answer is not None,
                    "answer": p.answer,
                    "earned": p.earned,
                    "elapsed_ms": p.elapsed_ms,
                    "joined": p.joined,
                }
                for p in self.players.values()
            ]
        else:
            assert player is not None
            payload["you"] = {
                "name": player.name,
                "score": player.round_start_score if self.status == "live" else player.score,
                "answer": player.answer,
                "earned": player.earned if self.status == "results" else None,
                "was_correct": player.answer == self.correct_index
                if self.status == "results" and player.answer is not None
                else None,
            }
            # The answer key must not be sent to a student before results.
            if self.status == "results":
                payload["correct_index"] = self.correct_index
        if self.status == "results":
            payload["leaderboard"] = [
                {"rank": i, "name": p.name, "score": p.score, "earned": p.earned}
                for i, p in enumerate(
                    sorted(self.players.values(), key=lambda p: (-p.score, p.joined)), 1
                )
            ]
        return payload

    def join(self, raw_name: object) -> tuple[str, str]:
        if not isinstance(raw_name, str):
            raise APIError(400, "이름을 입력해 주세요.")
        name = " ".join(raw_name.strip().split())
        if not 1 <= len(name) <= 20:
            raise APIError(400, "이름 또는 별명은 1~20자로 적어 주세요.")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
            raise APIError(400, "사용할 수 없는 문자가 포함되어 있습니다.")
        if len(self.players) >= MAX_ROOM_SIZE:
            raise APIError(409, "이 방은 정원이 가득 찼습니다.")
        if any(p.name.casefold() == name.casefold() for p in self.players.values()):
            raise APIError(409, "같은 이름이 이미 있습니다. 이름 뒤에 숫자를 붙여 주세요.")
        token = secrets.token_urlsafe(32)
        player = Player(token=token, name=name, joined=len(self.players) + 1)
        self.players[token] = player
        self.changed()
        return token, name

    def set_draft(self, data: dict):
        if self.status != "draft":
            raise APIError(409, "진행 중인 퀴즈는 수정할 수 없습니다.")
        revision = data.get("revision", -1)
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise APIError(400, "문제 수정 번호가 올바르지 않습니다.")
        if revision < self.draft_revision:
            return  # Late network request; don't overwrite newer typing.
        self._validate_and_store_draft(data)
        self.draft_revision = revision
        self.changed()

    def _validate_and_store_draft(self, data: dict):
        q = data.get("question", "")
        opts = data.get("options", [])
        key = data.get("correct_index")
        raw_multiplier = data.get("multiplier", 1)
        try:
            if isinstance(raw_multiplier, bool) or not isinstance(raw_multiplier, (str, int, float)):
                raise ValueError("bad type")
            multiplier = Decimal(str(raw_multiplier))
            hundredths = multiplier * 100
            if (not multiplier.is_finite() or multiplier < Decimal("0.01")
                    or multiplier > Decimal("100") or hundredths != hundredths.to_integral_value()):
                raise ValueError("out of range/precision")
        except (ValueError, InvalidOperation):
            raise APIError(400, "점수 배수는 0.01~100 사이의 숫자로, 소수점 둘째 자리까지 입력해 주세요.")
        if not isinstance(q, str) or len(q) > 500:
            raise APIError(400, "문제는 최대 500자까지 입력할 수 있습니다.")
        if not isinstance(opts, list) or len(opts) != 4 or any(not isinstance(o, str) or len(o) > 160 for o in opts):
            raise APIError(400, "보기를 4개까지, 각 160자 이내로 입력해 주세요.")
        if key is not None and (type(key) is not int or not 0 <= key <= 3):
            raise APIError(400, "정답 보기를 선택해 주세요.")
        self.question = q
        self.options = opts[:]
        self.correct_index = key
        self.multiplier_hundredths = int(hundredths)

    def start(self, data: dict):
        if self.status != "draft":
            raise APIError(409, "이미 시작된 문제입니다.")
        # This atomically commits the last keystrokes, even if a debounce timer is pending.
        self._validate_and_store_draft(data)
        if not self.question.strip():
            raise APIError(400, "문제를 먼저 입력해 주세요.")
        if sum(bool(o.strip()) for o in self.options) < 2:
            raise APIError(400, "보기는 최소 2개를 입력해 주세요.")
        if self.correct_index is None or not self.options[self.correct_index].strip():
            raise APIError(400, "정답인 보기를 교사 화면에서 선택해 주세요.")
        if not self.players:
            raise APIError(400, "학생이 최소 1명 입장한 뒤 시작할 수 있습니다.")
        self.round_no += 1
        self.status = "live"
        self.started_ns = time.monotonic_ns()
        self.started_at = int(time.time() * 1000)
        for p in self.players.values():
            p.round_start_score = p.score
            p.answer = None
            p.elapsed_ms = None
            p.earned = 0
        self.changed()

    def submit_answer(self, player: Player, data: dict):
        if self.status != "live":
            raise APIError(409, "지금은 답안을 제출할 수 없습니다.")
        choice = data.get("choice")
        if type(choice) is not int or not 0 <= choice < 4 or not self.options[choice].strip():
            raise APIError(400, "올바른 보기를 선택해 주세요.")
        if player.answer is not None:
            raise APIError(409, "이미 답안을 제출했습니다. 한 문제에 한 번만 제출할 수 있습니다.")
        assert self.started_ns is not None
        elapsed_ms = max(0, (time.monotonic_ns() - self.started_ns) // 1_000_000)
        player.answer = choice
        player.elapsed_ms = elapsed_ms
        if choice == self.correct_index:
            # 1,000 base + up to 500 time bonus, decreasing over first 30 seconds.
            raw_points = 1000 + max(0, 500 - elapsed_ms // 60)
            # Apply this round's multiplier to both base points and speed bonus; half-up.
            player.earned = (raw_points * self.multiplier_hundredths + 50) // 100
            player.score += player.earned
            player.correct_rounds += 1
            player.total_correct_ms += elapsed_ms
        self.changed()

    def close(self):
        if self.status != "live":
            raise APIError(409, "현재 진행 중인 퀴즈가 없습니다.")
        # Deterministic tiny score adjustments guarantee distinct final totals.
        # When original totals match, faster cumulative correct-response time wins.
        ordered = sorted(
            self.players.values(),
            key=lambda p: (p.score, p.correct_rounds, -p.total_correct_ms, -p.joined),
        )
        previous = -1
        for p in ordered:
            adjusted = max(p.score, previous + 1)
            p.earned += adjusted - p.score
            p.score = adjusted
            previous = adjusted
        self.status = "results"
        self.changed()

    def next(self):
        if self.status != "results":
            raise APIError(409, "먼저 현재 퀴즈를 종료해 주세요.")
        self.status = "draft"
        self.question = ""
        self.options = ["", "", "", ""]
        self.correct_index = None
        self.multiplier_hundredths = 100
        self.draft_revision = -1
        self.started_at = None
        self.started_ns = None
        for p in self.players.values():
            p.round_start_score = p.score
            p.answer = None
            p.elapsed_ms = None
            p.earned = 0
        self.changed()


def room_or_error(code: str) -> Room:
    if not code or code not in ROOMS:
        raise APIError(404, "방을 찾지 못했습니다. 코드를 확인해 주세요.")
    return ROOMS[code]


def create_room() -> Room:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    with ROOMS_LOCK:
        now = time.time()
        for code, room in tuple(ROOMS.items()):
            if now - room.touched_at > ROOM_TTL_SECONDS:
                del ROOMS[code]
        for _ in range(100):
            code = "".join(secrets.choice(alphabet) for _ in range(6))
            if code not in ROOMS:
                room = Room(code=code, teacher_token=secrets.token_urlsafe(40))
                ROOMS[code] = room
                return room
    raise APIError(503, "방을 생성할 수 없습니다. 다시 시도해 주세요.")


class QuizHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TokTokQuiz/1.0"

    def log_message(self, format, *args):
        LOGGER.info("%s %s", self.client_address[0], format % args)

    def respond(self, status: int, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def error(self, exception: Exception):
        if isinstance(exception, APIError):
            self.respond(exception.status, {"error": exception.message})
        else:
            LOGGER.exception("Request failure")
            self.respond(500, {"error": "서버 오류가 발생했습니다."})

    def get_json(self) -> dict:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > 200_000:
                raise APIError(413, "입력 내용이 너무 깁니다.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("not object")
            return data
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(400, "요청 형식이 올바르지 않습니다.")

    def token(self, query: dict | None = None) -> str:
        if query is not None:
            return query.get("token", [""])[0]
        return self.headers.get("X-Session-Token", "")

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/")
            if path == "/api/rooms":
                room = create_room()
                return self.respond(201, {"code": room.code, "token": room.teacher_token})
            parts = path.split("/")
            if len(parts) != 5 or parts[:3] != ["", "api", "rooms"]:
                raise APIError(404, "존재하지 않는 주소입니다.")
            code, action = parts[3].upper(), parts[4]
            with ROOMS_LOCK:
                room = room_or_error(code)
            data = self.get_json()
            with room.lock:
                if action == "join":
                    token, name = room.join(data.get("name"))
                    return self.respond(200, {"code": code, "token": token, "name": name})
                token = self.token()
                if action == "answer":
                    player = room.must_be_student(token)
                    room.submit_answer(player, data)
                elif action in {"draft", "start", "close", "next"}:
                    room.must_be_teacher(token)
                    if action == "draft":
                        room.set_draft(data)
                    elif action == "start":
                        room.start(data)
                    elif action == "close":
                        room.close()
                    else:
                        room.next()
                else:
                    raise APIError(404, "존재하지 않는 기능입니다.")
                return self.respond(200, {"ok": True, "version": room.version})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as ex:
            self.error(ex)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/health":
                return self.respond(200, {"ok": True})
            parts = path.split("/")
            if len(parts) == 5 and parts[:3] == ["", "api", "rooms"]:
                with ROOMS_LOCK:
                    room = room_or_error(parts[3].upper())
                query = parse_qs(parsed.query)
                token = self.token(query)
                with room.lock:
                    room.authenticate(token)
                    if parts[4] == "state":
                        return self.respond(200, room.snapshot(token))
                if parts[4] == "events":
                    return self.stream(room, token)
                raise APIError(404, "존재하지 않는 주소입니다.")
            if path in {"/", "/teacher", "/play", "/join"}:
                return self.static_file(STATIC / "index.html")
            assets = {"/styles.css": "styles.css", "/client.js": "client.js"}
            if path in assets:
                return self.static_file(STATIC / assets[path])
            raise APIError(404, "주소를 찾지 못했습니다.")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as ex:
            self.error(ex)

    def static_file(self, path: Path):
        payload = path.read_bytes()
        ctype = "text/html" if path.suffix == ".html" else "text/css" if path.suffix == ".css" else "application/javascript"
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(payload)

    def stream(self, room: Room, token: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        listener = threading.Event()
        with room.lock:
            room.listeners.add(listener)
        try:
            while True:
                with room.lock:
                    snapshot = room.snapshot(token)
                payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                self.wfile.write(("data: " + payload + "\n\n").encode("utf-8"))
                self.wfile.flush()
                signaled = listener.wait(timeout=12)
                listener.clear()
                if not signaled:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with room.lock:
                room.listeners.discard(listener)


class QuizServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def discover_lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "[컴퓨터의 내부 IP]"


def main():
    arg = argparse.ArgumentParser(description="톡톡 라이브 퀴즈")
    arg.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    arg.add_argument("--port", type=int, default=int(os.getenv("PORT", "8787")))
    args = arg.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    server = QuizServer((args.host, args.port), QuizHandler)
    print("\n🎉 톡톡 라이브 퀴즈 서버가 시작되었습니다!", flush=True)
    print(f"교사 컴퓨터: http://localhost:{args.port}", flush=True)
    print(f"같은 와이파이 학생: http://{discover_lan_ip()}:{args.port}", flush=True)
    print("종료하려면 Ctrl+C를 누르세요.\n", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
