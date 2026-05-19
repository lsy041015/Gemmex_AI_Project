"""
TUI AI Coder — Gemma-4-31B 기반 코딩 에이전트
textual TUI + Tool Use (read/write/run/git) + Agent Loop
"""
import os, sys, json, re, subprocess, threading, time, warnings, logging, shutil, difflib
from rich.markup import escape as _markup_escape
from pathlib import Path
from datetime import datetime
from itertools import islice
from config.key_loader import load_api_keys

warnings.filterwarnings("ignore", message=".*AFC.*")
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

from google import genai
from google.genai import types

from textual import work, events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll, Horizontal, Vertical
from textual.message import Message
from textual.widgets import (
    Static, Label, ContentSwitcher, Input, TextArea
)
from textual.reactive import reactive
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.console import Group

# ── 설정 ───────────────────────────────────────────────────────────
_ROOT       = Path(__file__).parent
MANUAL_PATH = next(
    (p for p in [_ROOT / "GEMMA_MANUAL.md", _ROOT / "skill-creator" / "GEMMA_MANUAL.md"] if p.exists()),
    _ROOT / "skill-creator" / "GEMMA_MANUAL.md",
)
SESSIONS_DIR = _ROOT / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

_API_KEYS = load_api_keys(Path(__file__).resolve())
if not _API_KEYS:
    raise RuntimeError("No API key found. Set GEMMA_API_KEY/GEMMA_API_KEYS or config key files.")

FIXED_MODEL = "gemma-4-31b-it"
AVAILABLE_MODELS = [
    "gemma-4-31b-it",
    "gemma-4-12b-it",
    "gemma-3-27b-it",
]

MAX_HISTORY    = 40
MAX_AUTO_RETRY = 5     # 자동 수정 최대 재시도 횟수
EXEC_TIMEOUT   = 30    # 코드 실행 타임아웃 (초)
SANDBOX_DIR    = Path("/tmp/gemma_sandbox")
SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
BASE_TEMPERATURE = 0.2
RETRY_TEMPERATURE_STEP = 0.15
MAX_REPAIR_CONTEXT = 1800

_DANGEROUS = re.compile(
    r'\b(rm\s+-[rf]|dd\s+if=|mkfs|chmod\s+777|sudo\s+rm)\b', re.IGNORECASE
)


class _KeyPool:
    def __init__(self, keys: list[str]):
        self._keys = keys
        self._rr = 0
        self._until = [0.0] * len(keys)
        self._lock = threading.Lock()

    def get(self) -> tuple[str, int]:
        with self._lock:
            now = time.time()
            n = len(self._keys)
            for _ in range(n):
                i = self._rr % n
                self._rr += 1
                if now >= self._until[i]:
                    return self._keys[i], i
            i = min(range(n), key=lambda x: self._until[x])
            return self._keys[i], i

    def block(self, idx: int, secs: float = 62.0) -> None:
        with self._lock:
            self._until[idx] = time.time() + secs

    def available(self) -> int:
        now = time.time()
        return sum(1 for t in self._until if now >= t)

    def __len__(self) -> int:
        return len(self._keys)


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()

# ── Tool 함수 ──────────────────────────────────────────────────────
def _read_file(path: str) -> str:
    p = Path(path).expanduser()
    try:
        if p.stat().st_size > 100_000:
            return f"[큰 파일 일부만 표시]\n" + p.read_text(errors="replace")[:3000]
        return p.read_text(errors="replace")
    except OSError as e:
        return f"오류: {e}"

def _write_file(path: str, content: str) -> str:
    p = Path(path).expanduser()
    bak_note = ""
    if p.exists():
        bak = p.with_suffix(p.suffix + ".bak")
        shutil.copy2(p, bak)
        bak_note = f"  (백업: {bak.name})"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"저장 완료: {p}{bak_note}"

def _list_dir(path: str = ".") -> str:
    p = Path(path).expanduser()
    try:
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        lines = [f"{'[D]' if e.is_dir() else '[F]'} {e.name}" for e in entries]
        return "\n".join(lines) or "(비어 있음)"
    except OSError as e:
        return f"오류: {e}"

def _find_files(pattern: str, directory: str = ".") -> str:
    base = Path(directory).expanduser()
    matches = [str(p) for p in islice(base.rglob(pattern), 50)]
    return "\n".join(matches) if matches else "일치하는 파일 없음"

def _search_in_files(keyword: str, directory: str = ".", extension: str = "") -> str:
    cmd = ["grep", "-rn"]
    if extension:
        cmd += ["--include", f"*{extension}"]
    cmd += [keyword, str(Path(directory).expanduser())]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    lines = result.stdout.strip().splitlines()[:30]
    return "\n".join(lines) if lines else "일치하는 내용 없음"

def _edit_file(path: str, old_string: str, new_string: str) -> str:
    p = Path(path).expanduser()
    try:
        content = p.read_text(encoding="utf-8")
    except OSError as e:
        return f"오류: {e}"
    count = content.count(old_string)
    if count == 0:
        return "오류: 텍스트를 찾을 수 없습니다."
    if count > 1:
        return f"오류: {count}곳에서 동일 텍스트 발견. 더 많은 컨텍스트를 포함하세요."
    bak = p.with_suffix(p.suffix + ".bak")
    shutil.copy2(p, bak)
    p.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
    return f"수정 완료: {p}  (백업: {bak.name})"

def _run_command(command: str, cwd: str = ".") -> str:
    if _DANGEROUS.search(command):
        return f"[보안] 위험 명령 차단: {command}"
    work_dir = Path(cwd).expanduser().resolve()
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True,
        timeout=60, cwd=str(work_dir)
    )
    output = (result.stdout + result.stderr).strip() or "(출력 없음)"
    if len(output) > 3000:
        output = output[:3000] + f"\n... ({len(output)}자 중 3000자)"
    return output

def _git_status(cwd: str = ".") -> str:
    r = subprocess.run(["git", "status", "--short"], capture_output=True, text=True,
                       timeout=10, cwd=str(Path(cwd).resolve()))
    return r.stdout.strip() or "(변경 없음)"

def _git_diff(cwd: str = ".", staged: bool = False) -> str:
    cmd = ["git", "diff"] + (["--staged"] if staged else [])
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=10, cwd=str(Path(cwd).resolve()))
    out = r.stdout.strip() or "(변경 없음)"
    return out[:4000] + ("..." if len(out) > 4000 else "")

# ── Step 3: 자동 실행 / 에러 감지 / 자동 수정 헬퍼 ──────────────────
_CODE_BLOCK_RE = re.compile(r'```(\w*)\n(.*?)```', re.DOTALL)

_ERROR_PATTERNS = re.compile(
    r'(Traceback \(most recent call last\)|'
    r'(?:Module|Import|Name|Type|Value|Attribute|Key|Index|File|Permission|Syntax|'
    r'Runtime|OS|IO|Zero|Overflow|Memory)Error[:\s]|'
    r'Exception:|raise \w+Error)',
    re.IGNORECASE,
)

_RESPONSE_TEMPLATE = (
    "코딩/수정 요청에는 아래 섹션 제목을 반드시 포함하세요: "
    "계획, 변경 파일, 패치, 검증 명령, 결과."
)

def _extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """마크다운에서 (언어, 코드) 목록 추출."""
    return [(m.group(1).lower() or "python", m.group(2).strip())
            for m in _CODE_BLOCK_RE.finditer(text)]

def _is_error(output: str) -> bool:
    return bool(_ERROR_PATTERNS.search(output))


def _is_coding_query(text: str) -> bool:
    lower = text.lower()
    keys = ("코드", "수정", "버그", "오류", "테스트", "리팩토링", "python", "fix", "debug", "pytest")
    return any(k in lower for k in keys)


def _has_structured_sections(text: str) -> bool:
    required = ("계획", "변경 파일", "패치", "검증", "결과")
    return all(r in text for r in required)


def _classify_runtime_error(output: str) -> str:
    o = output.lower()
    if "modulenotfounderror" in o or "no module named" in o:
        return "import_error"
    if "syntaxerror" in o or "indentationerror" in o:
        return "syntax_error"
    if "typeerror" in o or "attributeerror" in o:
        return "type_error"
    if "assertionerror" in o:
        return "assertion_error"
    if "indexerror" in o or "keyerror" in o:
        return "index_key_error"
    if "timeout" in o or "타임아웃" in o:
        return "timeout"
    return "runtime_error"


def _truncate_text(text: str, limit: int = MAX_REPAIR_CONTEXT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...({len(text)}자 중 {limit}자)"


def _build_repair_prompt(output: str, retry_count: int, lang: str, code: str) -> str:
    err_type = _classify_runtime_error(output)
    strategy_map = {
        "import_error": "누락 의존성 또는 import 경로를 먼저 바로잡고, 나머지 로직은 그대로 유지하세요.",
        "syntax_error": "문법/들여쓰기 문제를 먼저 해결하고, 실행 가능한 완전한 코드만 다시 제시하세요.",
        "type_error": "자료형 가정과 함수/메서드 호출부를 다시 점검하고, 입력 경계조건까지 보정하세요.",
        "assertion_error": "테스트 기대값과 어긋난 논리 버그입니다. 최소 수정만 고집하지 말고 알고리즘 자체를 다시 점검하세요.",
        "index_key_error": "빈 입력, 범위 초과, 누락 키를 방어하도록 조건 분기를 보강하세요.",
        "timeout": "비효율적인 반복/재귀를 줄이고 더 나은 알고리즘이나 가지치기로 수정하세요.",
        "runtime_error": "실패 원인을 먼저 특정한 뒤, 동일 증상이 재발하지 않도록 구조를 바로잡으세요.",
    }
    focus = strategy_map.get(err_type, strategy_map["runtime_error"])
    return (
        f"이전 코드가 샌드박스 실행에 실패했습니다. {retry_count}/{MAX_AUTO_RETRY} 재시도입니다.\n"
        f"실패 유형: {err_type}\n"
        f"수정 우선순위: {focus}\n\n"
        f"실패한 {lang} 코드:\n"
        f"```{lang}\n{_truncate_text(code)}\n```\n\n"
        f"실행 오류/실패 로그:\n"
        f"```\n{_truncate_text(output)}\n```\n\n"
        "다음 규칙을 반드시 지키세요:\n"
        "1) 원인을 한 줄로 진단하세요.\n"
        "2) 수정된 전체 코드를 하나의 코드블록으로 제시하세요.\n"
        "3) AssertionError 또는 논리 오류면 기존 접근을 버리고 더 정확한 알고리즘으로 교체해도 됩니다.\n"
        "4) 추측성 설명은 줄이고, 실행 가능한 최종 코드만 남기세요.\n"
        "5) 마지막에 재발 방지 포인트를 한 줄로 적으세요."
    )

def _classify_exception_message(e: Exception) -> str:
    msg = str(e).lower()
    if "429" in msg or "resource_exhausted" in msg or "quota" in msg:
        return "레이트리밋/쿼터"
    if "timeout" in msg or "deadline" in msg:
        return "타임아웃"
    if "permission" in msg or "forbidden" in msg or "401" in msg or "403" in msg:
        return "권한/인증"
    if "network" in msg or "connection" in msg or "dns" in msg or "unreachable" in msg:
        return "네트워크"
    return "기타"

def _is_runnable(code: str) -> bool:
    """독립 실행 가능한 스크립트인지 휴리스틱 판단."""
    lines = [l.strip() for l in code.splitlines()
             if l.strip() and not l.strip().startswith('#')]
    if len(lines) < 2:
        return False
    # 함수/클래스 정의만 있고 호출부가 없으면 제외
    non_def = [l for l in lines
               if not l.startswith(('def ', 'class ', 'import ', 'from ', '@', '#'))]
    return len(non_def) >= 1

def _extract_missing_package(error: str) -> str | None:
    m = re.search(r"No module named '([^']+)'", error)
    return m.group(1).split('.')[0] if m else None

def _auto_install(package: str) -> str:
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", package, "-q"],
        capture_output=True, text=True, timeout=60,
    )
    out = (r.stdout + r.stderr).strip()
    return out[:400] if out else "설치 완료"

def _run_in_sandbox(code: str, lang: str = "python") -> str:
    """샌드박스 디렉토리에서 코드를 실행하고 결과 반환."""
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".py" if lang in ("python", "python3") else ".sh"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    script = SANDBOX_DIR / f"auto_run_{stamp}{ext}"
    script.write_text(code, encoding="utf-8")
    interpreter = "python3" if lang in ("python", "python3") else "bash"
    try:
        r = subprocess.run(
            [interpreter, str(script)],
            capture_output=True, text=True,
            timeout=EXEC_TIMEOUT, cwd=str(SANDBOX_DIR),
        )
        out = (r.stdout + r.stderr).strip() or "(출력 없음)"
        return out[:2000] + (f"\n...({len(out)}자 중 2000자)" if len(out) > 2000 else "")
    except subprocess.TimeoutExpired:
        return f"[타임아웃] {EXEC_TIMEOUT}초 초과 — 강제 종료"
    except Exception as e:
        return f"실행 오류: {e}"
    finally:
        try:
            script.unlink(missing_ok=True)
        except Exception:
            pass


def _compute_diff(path: str, new_content: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return "(새 파일)"
    try:
        old_lines = p.read_text(errors="replace").splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{p.name}", tofile=f"b/{p.name}", n=3,
        ))
        if not diff:
            return "(변경 없음)"
        result = "".join(diff[:60])
        if len(diff) > 60:
            result += f"\n... ({len(diff) - 60}줄 생략)"
        return result
    except Exception as e:
        return f"(diff 오류: {e})"


def _render_rich(text: str):
    """응답 텍스트를 Markdown + 코드블록 Syntax 혼합 렌더링"""
    parts = []
    last = 0
    for m in _CODE_BLOCK_RE.finditer(text):
        pre = text[last:m.start()]
        if pre.strip():
            parts.append(Markdown(pre))
        lang = (m.group(1).strip() or "text").lower()
        code = m.group(2)
        parts.append(Syntax(code, lang, theme="monokai", word_wrap=True,
                             background_color="#101722"))
        last = m.end()
    post = text[last:]
    if post.strip():
        parts.append(Markdown(post))
    if not parts:
        return Markdown(text)
    return Group(*parts)


def _build_approval_preview(name: str, args: dict) -> str:
    if name == "write_file":
        path = args.get("path", "?")
        content = args.get("content", "")
        diff = _compute_diff(path, content)
        return f"[write_file] {path}\n```diff\n{diff}\n```\n승인? (y/n)"
    elif name == "edit_file":
        path = args.get("path", "?")
        old = (args.get("old_string") or "")[:200]
        new = (args.get("new_string") or "")[:200]
        return (f"[edit_file] {path}\n"
                f"```diff\n- {old}\n+ {new}\n```\n승인? (y/n)")
    elif name == "run_command":
        cmd = args.get("command", "?")
        return f"[run_command] `{cmd}`\n실행 승인? (y/n)"
    return f"[{name}] 실행 승인? (y/n)"


_APPROVAL_TOOLS = {"write_file", "edit_file", "run_command"}

TOOL_FN = {
    "read_file":       _read_file,
    "write_file":      _write_file,
    "list_dir":        _list_dir,
    "find_files":      _find_files,
    "search_in_files": _search_in_files,
    "edit_file":       _edit_file,
    "run_command":     _run_command,
    "git_status":      _git_status,
    "git_diff":        _git_diff,
}

# ── Gemma API Tool 선언 ────────────────────────────────────────────
FS_DECLARATIONS = types.Tool(function_declarations=[
    types.FunctionDeclaration(name="read_file",
        description="파일 내용을 읽습니다.",
        parameters=types.Schema(type=types.Type.OBJECT,
            properties={"path": types.Schema(type=types.Type.STRING)},
            required=["path"])),
    types.FunctionDeclaration(name="write_file",
        description="파일에 내용을 씁니다.",
        parameters=types.Schema(type=types.Type.OBJECT,
            properties={
                "path":    types.Schema(type=types.Type.STRING),
                "content": types.Schema(type=types.Type.STRING),
            }, required=["path", "content"])),
    types.FunctionDeclaration(name="list_dir",
        description="디렉토리 내용을 나열합니다.",
        parameters=types.Schema(type=types.Type.OBJECT,
            properties={"path": types.Schema(type=types.Type.STRING)},
            required=["path"])),
    types.FunctionDeclaration(name="find_files",
        description="파일 패턴으로 검색합니다.",
        parameters=types.Schema(type=types.Type.OBJECT,
            properties={
                "pattern":   types.Schema(type=types.Type.STRING),
                "directory": types.Schema(type=types.Type.STRING),
            }, required=["pattern"])),
    types.FunctionDeclaration(name="search_in_files",
        description="파일 내용에서 키워드를 검색합니다.",
        parameters=types.Schema(type=types.Type.OBJECT,
            properties={
                "keyword":   types.Schema(type=types.Type.STRING),
                "directory": types.Schema(type=types.Type.STRING),
                "extension": types.Schema(type=types.Type.STRING),
            }, required=["keyword"])),
    types.FunctionDeclaration(name="edit_file",
        description="파일의 특정 텍스트를 수정합니다.",
        parameters=types.Schema(type=types.Type.OBJECT,
            properties={
                "path":       types.Schema(type=types.Type.STRING),
                "old_string": types.Schema(type=types.Type.STRING),
                "new_string": types.Schema(type=types.Type.STRING),
            }, required=["path", "old_string", "new_string"])),
    types.FunctionDeclaration(name="run_command",
        description="쉘 명령을 실행합니다.",
        parameters=types.Schema(type=types.Type.OBJECT,
            properties={
                "command": types.Schema(type=types.Type.STRING),
                "cwd":     types.Schema(type=types.Type.STRING),
            }, required=["command"])),
    types.FunctionDeclaration(name="git_status",
        description="git 상태를 확인합니다.",
        parameters=types.Schema(type=types.Type.OBJECT,
            properties={"cwd": types.Schema(type=types.Type.STRING)},
            required=[])),
    types.FunctionDeclaration(name="git_diff",
        description="git diff를 확인합니다.",
        parameters=types.Schema(type=types.Type.OBJECT,
            properties={
                "cwd":    types.Schema(type=types.Type.STRING),
                "staged": types.Schema(type=types.Type.BOOLEAN),
            }, required=[])),
])

# ── 시스템 프롬프트 ────────────────────────────────────────────────
def _load_system_prompt() -> str:
    if MANUAL_PATH.exists():
        return MANUAL_PATH.read_text(encoding="utf-8")
    return "당신은 코딩 전문 AI 어시스턴트입니다. 한국어로 답변합니다."

def _build_context() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cwd = Path.cwd()
    return f"\n\n현재 시각: {now}\n작업 디렉토리: {cwd}"

# ── TUI 위젯 ───────────────────────────────────────────────────────
class MessageWidget(Static):
    """단일 메시지 버블"""
    def __init__(self, role: str, content: str, **kw):
        super().__init__(**kw)
        self.role = role
        self._msg = content  # Static.content 프로퍼티 충돌 방지

    def on_mount(self):
        msg = _markup_escape(self._msg)
        if self.role == "user":
            self.update(f"[#4ec9b0]>[/] {msg}")
        elif self.role == "tool":
            self.update(f"[#4a4a4a]{msg}[/]")
        elif self.role == "system":
            self.update(f"[#dcdcaa]{msg}[/]")
        elif self.role == "retry":
            self.update(f"[#555555]{msg}[/]")
        elif self.role == "error":
            self.update(f"[#f48771]{msg}[/]")
        else:
            self.update(_render_rich(self._msg))

    def get_css_class(self) -> str:
        return self.role


class MultilineInput(TextArea):
    """Enter 제출 / Ctrl+J 줄바꿈 / Ctrl+P·Ctrl+N 히스토리"""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._hist: list[str] = []
        self._hist_idx: int = -1
        self._draft: str = ""

    def record(self, text: str) -> None:
        if text and (not self._hist or self._hist[-1] != text):
            self._hist.append(text)
        self._hist_idx = -1
        self._draft = ""

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
                self.clear()
            event.prevent_default()
            event.stop()
        elif event.key in ("ctrl+j", "shift+enter"):
            self.insert("\n")
            event.prevent_default()
            event.stop()
        elif event.key == "ctrl+p":
            if not self._hist:
                return
            if self._hist_idx == -1:
                self._draft = self.text
                self._hist_idx = len(self._hist) - 1
            elif self._hist_idx > 0:
                self._hist_idx -= 1
            self.load_text(self._hist[self._hist_idx])
            event.prevent_default()
            event.stop()
        elif event.key == "ctrl+n":
            if self._hist_idx == -1:
                return
            if self._hist_idx < len(self._hist) - 1:
                self._hist_idx += 1
                self.load_text(self._hist[self._hist_idx])
            else:
                self._hist_idx = -1
                self.load_text(self._draft)
            event.prevent_default()
            event.stop()


class StatusBar(Static):
    """하단 상태 표시줄 (Codex 스타일)"""
    model      = reactive(FIXED_MODEL)
    thinking   = reactive(False)
    tokens     = reactive((0, 0))
    retry      = reactive(0)
    auto_exec  = reactive(True)
    key_status = reactive("")
    cwd_str    = reactive("")

    def render(self):
        in_tok, out_tok = self.tokens
        think_str = "  [#dcdcaa]thinking...[/]" if self.thinking else ""
        retry_str = f"  [#dcdcaa]retry {self.retry}/{MAX_AUTO_RETRY}[/]" if self.retry > 0 else ""
        key_str   = f"  [#3a3a3a]{self.key_status}[/]" if self.key_status else ""
        auto_str  = "  [#3a3a3a]auto:off[/]" if not self.auto_exec else ""
        tok       = in_tok + out_tok
        tok_str   = f"  [#2e2e2e]{tok:,}tok[/]" if tok > 0 else ""
        return (
            f"[#4ec9b0]{self.model}[/] [#2e2e2e]·[/] [#444444]{self.cwd_str}[/]"
            f"{think_str}{retry_str}{key_str}{auto_str}{tok_str}"
            f"  [#2e2e2e]^J줄바꿈 ^P이전 ^B패널[/]"
        )


SIDE_SESSIONS = "side-sessions"
SIDE_TOOLS    = "side-tools"
_SIDE_ORDER   = [SIDE_SESSIONS, SIDE_TOOLS]
_SIDE_LABELS  = {SIDE_SESSIONS: "세션", SIDE_TOOLS: "도구로그"}


class SidePanel(Vertical):
    """우측 사이드 패널 — 세션 목록 / 도구 로그"""
    current_tab: reactive[str] = reactive(SIDE_SESSIONS)

    def compose(self) -> ComposeResult:
        yield Static("", id="side-header", markup=True)
        with ContentSwitcher(initial=SIDE_SESSIONS, id="side-sw"):
            yield VerticalScroll(id=SIDE_SESSIONS)
            yield VerticalScroll(id=SIDE_TOOLS)

    def on_mount(self) -> None:
        self._update_header()

    def _update_header(self) -> None:
        parts = [
            f"[bold #8bd5ff]{lbl}[/]" if key == self.current_tab else f"[#8a97a8]{lbl}[/]"
            for key, lbl in _SIDE_LABELS.items()
        ]
        self.query_one("#side-header", Static).update(" | ".join(parts))

    def watch_current_tab(self, tab: str) -> None:
        try:
            self.query_one("#side-sw", ContentSwitcher).current = tab
            self._update_header()
        except Exception:
            pass

    def cycle_tab(self) -> None:
        idx = _SIDE_ORDER.index(self.current_tab) if self.current_tab in _SIDE_ORDER else 0
        self.current_tab = _SIDE_ORDER[(idx + 1) % len(_SIDE_ORDER)]

    def refresh_sessions(self) -> None:
        scroll = self.query_one(f"#{SIDE_SESSIONS}", VerticalScroll)
        scroll.remove_children()
        files = sorted(SESSIONS_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:20]
        if not files:
            scroll.mount(Static("[#8a97a8]세션 없음[/]", markup=True))
        else:
            for f in files:
                scroll.mount(Static(f"[#8bd5ff]{f.name}[/]", markup=True))

    def refresh_tools(self, tool_logs: list[dict]) -> None:
        scroll = self.query_one(f"#{SIDE_TOOLS}", VerticalScroll)
        scroll.remove_children()
        if not tool_logs:
            scroll.mount(Static("[#8a97a8]도구 호출 없음[/]", markup=True))
            return
        for i, log in enumerate(reversed(tool_logs[-30:]), 1):
            idx  = len(tool_logs) - i + 1
            args = _markup_escape((log.get("args") or "")[:40])
            prev = _markup_escape((log.get("result") or "")[:80].replace("\n", " "))
            scroll.mount(Static(
                f"[#94e2d5]{idx}. {log['name']}[/] [#8a97a8]{args}[/]\n[#8a97a8]{prev}[/]",
                markup=True,
            ))


class InfoPanel(Static):
    """>_ Gemmex 상단 정보 박스"""
    current_model: reactive[str] = reactive(FIXED_MODEL)

    def render(self):
        cwd = str(Path.cwd()).replace(str(Path.home()), "~")
        return (
            f"[bold #4ec9b0]>_ Gemmex[/]\n\n"
            f"[#444444]model:[/]     [bold #4ec9b0]{self.current_model}[/]"
            f"  [#333333]/model to change[/]\n"
            f"[#444444]directory:[/] [#777777]{cwd}[/]"
        )


class LoadingOverlay(Static):
    """Codex 스타일의 절제된 로딩 패널"""
    message = reactive("모델 로딩 중")
    detail = reactive("요청을 처리하고 있습니다")
    frame = reactive(0)

    _FRAMES = ("∙∙∙", "●∙∙", "∙●∙", "∙∙●")

    def on_mount(self) -> None:
        self.display = False
        self.set_interval(0.25, self._advance_frame)

    def _advance_frame(self) -> None:
        if self.display:
            self.frame = (self.frame + 1) % len(self._FRAMES)

    def watch_message(self, _: str) -> None:
        self.update(self.render())

    def watch_detail(self, _: str) -> None:
        self.update(self.render())

    def watch_frame(self, _: int) -> None:
        if self.display:
            self.update(self.render())

    def render(self):
        spinner = self._FRAMES[self.frame]
        return (
            f"[bold #4ec9b0]>_ Gemmex[/]\n"
            f"[#8a97a8]{spinner}[/] [bold #e6e6e6]{self.message}[/]\n"
            f"[#666666]{self.detail}[/]\n"
            f"[#444444]Esc cancel[/]"
        )


# ── 메인 앱 ────────────────────────────────────────────────────────
class TuiCoder(App):
    CSS = """
    Screen {
        background: #0c0c0c;
        color: #cccccc;
    }
    #app-body {
        width: 100%;
        height: 1fr;
    }
    #screen-root {
        width: 100%;
        height: 100%;
    }
    #main-column {
        width: 1fr;
        height: 100%;
    }
    InfoPanel {
        height: 6;
        border: round #2e2e2e;
        margin: 1 2 0 2;
        padding: 0 1;
        background: #0c0c0c;
    }
    #chat-scroll {
        height: 1fr;
        width: 100%;
        margin: 1 0 0 0;
        background: #0c0c0c;
        border: none;
    }
    MessageWidget {
        padding: 0 3;
        margin-bottom: 0;
        background: #0c0c0c;
    }
    MessageWidget.user {
        background: #141414;
        padding: 0 3;
    }
    MessageWidget.assistant {
        background: #0c0c0c;
    }
    MessageWidget.tool {
        color: #4a4a4a;
    }
    MessageWidget.system {
        color: #dcdcaa;
    }
    MessageWidget.error {
        color: #f48771;
    }
    MessageWidget.retry {
        color: #555555;
    }
    #input-row {
        height: auto;
        min-height: 3;
        max-height: 9;
        width: 100%;
        background: #141414;
        border-top: solid #222222;
    }
    #input-prompt {
        width: 3;
        height: auto;
        min-height: 3;
        background: #141414;
        color: #4ec9b0;
        padding: 1 0 0 1;
    }
    #user-input {
        width: 1fr;
        height: auto;
        min-height: 1;
        max-height: 7;
        border: none;
        background: #141414;
        color: #cccccc;
        padding: 0 1;
    }
    MultilineInput > .text-area--cursor-line {
        background: #1a1a1a;
    }
    StatusBar {
        height: 1;
        width: 100%;
        background: #0c0c0c;
        color: #444444;
        border-top: solid #222222;
        padding: 0 2;
    }
    #side-panel {
        display: none;
        width: 36;
        height: 100%;
        border-left: solid #222222;
        background: #0e0e0e;
    }
    #side-header {
        height: 1;
        background: #141414;
        padding: 0 1;
    }
    #side-sw {
        height: 1fr;
    }
    #side-sessions, #side-tools {
        height: 100%;
        padding: 0 1;
    }
    LoadingOverlay {
        layer: overlay;
        dock: top;
        width: 42;
        height: 6;
        margin: 9 0 0 0;
        align: center middle;
        content-align: left middle;
        background: #101010 92%;
        border: round #2a2a2a;
        color: #e6e6e6;
        text-align: left;
        padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("ctrl+c",  "quit",            "종료",    show=False),
        Binding("escape",  "cancel",          "취소",    show=False),
        Binding("ctrl+b",  "toggle_side",     "패널",    show=False),
        Binding("ctrl+t",  "cycle_side_tab",  "탭전환",  show=False),
    ]

    model     = reactive(FIXED_MODEL)
    thinking  = reactive(False)
    auto_exec = reactive(True)
    loading_message = reactive("대기 중")
    loading_detail = reactive("요청을 기다리는 중")

    def __init__(self):
        super().__init__()
        self.key_pool    = _KeyPool(_API_KEYS)
        self.client      = genai.Client(api_key=self.key_pool.get()[0])
        self.history: list[types.Content] = []
        self.total_in    = 0
        self.total_out   = 0
        self.tool_detail = False
        self.tool_logs: list[dict] = []
        self.cancel_requested = False
        self.keep_input_on_submit = False
        self.awaiting_pip_confirm: str | None = None
        self.pending_retry_payload: tuple[types.Content, str, int, str, str] | None = None
        self._approve_event  = threading.Event()
        self._approve_result: bool = False
        self._pending_approval: dict | None = None
        self._init_history()

    def _generate_content_with_key_rotation(self, model: str, contents, config):
        max_retries = max(3, min(8, len(self.key_pool) * 2))
        last_error = None
        for attempt in range(max_retries):
            key, key_idx = self.key_pool.get()
            self.client = genai.Client(api_key=key)
            try:
                return self.client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
            except Exception as e:
                last_error = e
                if _is_rate_limit_error(e) and attempt < max_retries - 1:
                    self.key_pool.block(key_idx)
                    wait = 1.5 if self.key_pool.available() > 0 else min(30.0, 2.0 * (2 ** attempt))
                    time.sleep(wait)
                    continue
                raise
        if last_error:
            raise last_error
        raise RuntimeError("generate_content failed without explicit error")

    def _init_history(self):
        system = _load_system_prompt() + _build_context()
        self.history = [
            types.Content(role="user",  parts=[types.Part(text=f"[시스템 지침]\n{system}")]),
            types.Content(role="model", parts=[types.Part(text="지침 숙지 완료. 무엇을 도와드릴까요?")]),
        ]

    def compose(self) -> ComposeResult:
        with Vertical(id="screen-root"):
            with Horizontal(id="app-body"):
                with Vertical(id="main-column"):
                    yield InfoPanel(id="info-panel")
                    yield VerticalScroll(id="chat-scroll")
                    with Horizontal(id="input-row"):
                        yield Static("[bold #4ec9b0]>[/] ", id="input-prompt", markup=True)
                        yield MultilineInput(id="user-input")
                    yield StatusBar(id="status-bar")
                yield SidePanel(id="side-panel")
            yield LoadingOverlay(id="loading-overlay")

    def on_mount(self):
        self._add_message("assistant", "안녕하세요! 코딩을 도와드릴게요.\n\n"
            "파일 읽기/쓰기, 코드 실행, Git 명령을 직접 수행할 수 있습니다.\n"
            "예) `main.py 파일을 읽고 버그를 찾아줘`\n"
            "명령어 목록은 `/help`를 입력하세요.")
        self._input().focus()
        self._refresh_input_placeholder()
        self._sync_status()

    def _input(self) -> MultilineInput:
        return self.query_one("#user-input", MultilineInput)

    def _refresh_input_placeholder(self):
        pass  # 상태바 힌트로 대체 (TextArea는 placeholder 미지원)

    def _sync_status(self, retry: int = 0):
        bar = self.query_one(StatusBar)
        bar.model      = self.model
        bar.thinking   = self.thinking
        bar.tokens     = (self.total_in, self.total_out)
        bar.retry      = retry
        bar.auto_exec  = self.auto_exec
        avail = self.key_pool.available()
        total = len(self.key_pool)
        bar.key_status = f"키:{avail}/{total}"
        bar.cwd_str    = str(Path.cwd()).replace(str(Path.home()), "~")
        try:
            self.query_one("#info-panel", InfoPanel).current_model = self.model
        except Exception:
            pass
        try:
            overlay = self.query_one("#loading-overlay", LoadingOverlay)
            overlay.message = self.loading_message
            overlay.detail = self.loading_detail
            overlay.display = self.thinking
            if self.thinking:
                overlay.update(overlay.render())
        except Exception:
            pass

    def _add_message(self, role: str, content: str):
        scroll  = self.query_one("#chat-scroll", VerticalScroll)
        widget  = MessageWidget(role, content, classes=role)
        scroll.mount(widget)
        scroll.scroll_end(animate=False)

    def on_multiline_input_submitted(self, event: "MultilineInput.Submitted") -> None:
        inp = self._input()
        text = event.text.strip()
        if not text:
            return
        self.keep_input_on_submit = False
        try:
            self._handle_user_input(text)
        except Exception as e:
            self._add_message("error", f"입력 처리 실패: {e}")
            self.keep_input_on_submit = True
        if not self.keep_input_on_submit:
            inp.record(text)
            inp.clear()

    def _handle_user_input(self, text: str):
        # 도구 실행 승인 대기 중
        if self._pending_approval is not None:
            self._approve_result = text.lower() in ('y', 'yes', '예', 'ㅇ')
            self._pending_approval = None
            self._approve_event.set()
            return

        if self.awaiting_pip_confirm:
            ans = text.lower()
            pkg = self.awaiting_pip_confirm
            if ans not in ("y", "yes", "예", "ㅇ", "n", "no", "아니오", "ㄴ"):
                self.keep_input_on_submit = True
                self._add_message("retry", f"`{pkg}` 설치 확인 대기 중입니다. `y` 또는 `n`을 입력하세요.")
                return
            self.awaiting_pip_confirm = None
            if ans in ("y", "yes", "예", "ㅇ"):
                self._add_message("retry", f"→ 패키지 설치 진행: pip install {pkg}")
                _auto_install(pkg)
                if self.pending_retry_payload:
                    candidate_content, output, retry_count, lang, code = self.pending_retry_payload
                    self.pending_retry_payload = None
                    self._continue_after_exec_error(candidate_content, output, retry_count, lang, code)
                    self.thinking = True
                    self._sync_status()
                    self._call_llm("이전 실행 오류를 반영해 수정을 계속 진행하세요.")
                return
            self._add_message("retry", f"패키지 설치 취소: {pkg} (수동 설치 후 다시 시도하세요)")
            self.pending_retry_payload = None
            return

        # 슬래시 명령
        if text == "/clear":
            self.query_one("#chat-scroll", VerticalScroll).remove_children()
            self._add_message("system", "대화 화면 초기화됨")
            return
        if text == "/help":
            self._add_message("assistant",
                "**명령어**\n"
                "- `/new` — 새 대화 시작\n"
                "- `/save` — 현재 세션 저장\n"
                "- `/cancel` — 현재 실행 취소 요청\n"
                "- `/quit` — 앱 종료\n"
                "- `/clear` — 화면 지우기\n"
                "- `/load <파일명>` — 세션 불러오기\n"
                "- `/sessions` — 최근 세션 목록\n"
                "- `/find <키워드>` — 대화 검색\n"
                "- `/model` 또는 `/model <번호|이름>` — 모델 선택\n"
                "- `/model next` — 다음 모델로 순환\n"
                "- `/tool <번호>` — 도구 로그 상세 보기\n"
                "- `/tooldetail` — 도구 로그 상세 출력 토글\n"
                "- `/auto` — 자동 실행 ON (코드 블록 자동 실행)\n"
                "- `/noauto` — 자동 실행 OFF\n"
                "- `!<명령>` — 셸 명령 직접 실행 (예: `!ls -la`)\n"
            )
            return
        if text == "/new":
            self.action_new_chat()
            return
        if text == "/save":
            self.action_save_session()
            return
        if text == "/cancel":
            self.action_cancel()
            return
        if text == "/quit":
            self.action_quit()
            return
        if text == "/sessions":
            files = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
            if not files:
                self._add_message("tool", "세션 파일 없음")
            else:
                body = "\n".join(f"- {p.name}" for p in files)
                self._add_message("tool", f"최근 세션:\n{body}")
            return
        if text.startswith("/load"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                self._add_message("error", "사용법: /load <파일명>")
                return
            self._load_session(parts[1].strip())
            return
        if text.startswith("/find "):
            kw = text[6:].strip().lower()
            if not kw:
                self._add_message("error", "사용법: /find <키워드>")
                return
            self._find_in_messages(kw)
            return
        if text == "/tooldetail":
            self.tool_detail = not self.tool_detail
            self._add_message("retry", f"도구 상세 로그 {'ON' if self.tool_detail else 'OFF'}")
            return
        if text.startswith("/tool "):
            idx = text[6:].strip()
            if not idx.isdigit():
                self._add_message("error", "사용법: /tool <번호>")
                return
            i = int(idx) - 1
            if i < 0 or i >= len(self.tool_logs):
                self._add_message("error", "해당 번호의 도구 로그가 없습니다.")
                return
            log = self.tool_logs[i]
            self._add_message("tool", f"[도구#{i+1}] {log['name']}({log['args']})\n{log['result']}")
            return
        if text == "/model":
            lines = [f"{i+1}. {m}{' (현재)' if m == self.model else ''}" for i, m in enumerate(AVAILABLE_MODELS)]
            self._add_message("assistant", "모델 선택:\n" + "\n".join(lines) + "\n`/model <번호|이름>` 입력")
            return
        if text.startswith("/model "):
            target = text.split(" ", 1)[1].strip()
            if target.lower() == "next":
                self.action_cycle_model()
                return
            self._set_model_from_input(target)
            return
        if text == "/auto":
            self.auto_exec = True
            self._sync_status()
            self._refresh_input_placeholder()
            self._add_message("retry", f"자동 실행 ON — 코드 블록을 샌드박스에서 자동 실행합니다.")
            return
        if text == "/noauto":
            self.auto_exec = False
            self._sync_status()
            self._refresh_input_placeholder()
            self._add_message("retry", f"자동 실행 OFF")
            return
        if text.startswith("!"):
            cmd = text[1:].strip()
            out = _run_command(cmd)
            self._add_message("tool", f"$ {cmd}\n{out}")
            return

        self._add_message("user", text)
        self.thinking = True
        self.loading_message = f"{self.model} 응답 생성 중"
        self.loading_detail = "모델이 컨텍스트를 읽고 답변을 작성하는 중"
        self.cancel_requested = False
        self._sync_status()
        self._call_llm(text)

    def _set_model_from_input(self, target: str):
        pick = None
        if target.isdigit():
            n = int(target)
            if 1 <= n <= len(AVAILABLE_MODELS):
                pick = AVAILABLE_MODELS[n - 1]
        else:
            for m in AVAILABLE_MODELS:
                if m == target:
                    pick = m
                    break
        if not pick:
            self._add_message("error", "모델을 찾을 수 없습니다. `/model`로 목록을 확인하세요.")
            return
        self.model = pick
        self.loading_message = f"{self.model} 로딩 중"
        self.loading_detail = "모델 전환 설정을 반영하는 중"
        self._sync_status()
        self._add_message("retry", f"모델 변경: {self.model}")

    def _load_session(self, filename: str):
        path = SESSIONS_DIR / filename
        if not path.exists():
            self._add_message("error", f"세션 파일 없음: {path.name}")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._init_history()
            self.query_one("#chat-scroll", VerticalScroll).remove_children()
            for item in data:
                role = item.get("role", "assistant")
                txt = item.get("text", "")
                if txt:
                    self._add_message("assistant" if role == "model" else role, txt)
                    self.history.append(types.Content(
                        role=("model" if role in ("assistant", "model") else "user"),
                        parts=[types.Part(text=txt)]
                    ))
            self._add_message("tool", f"세션 불러오기 완료: {path.name}")
        except Exception as e:
            self._add_message("error", f"세션 로드 실패: {e}")

    def _find_in_messages(self, keyword: str):
        matches = []
        for idx, c in enumerate(self.history[2:], start=1):
            if c.parts and hasattr(c.parts[0], "text") and c.parts[0].text:
                txt = c.parts[0].text
                if keyword in txt.lower():
                    preview = txt[:120].replace("\n", " ")
                    matches.append(f"{idx}. [{c.role}] {preview}")
        if not matches:
            self._add_message("tool", f"검색 결과 없음: {keyword}")
            return
        self._add_message("tool", "검색 결과:\n" + "\n".join(matches[:20]))

    def _continue_after_exec_error(
        self,
        candidate_content: types.Content,
        output: str,
        retry_count: int,
        lang: str,
        code: str,
    ):
        if retry_count >= MAX_AUTO_RETRY - 1:
            self.call_from_thread(self._add_message, "retry",
                                  f"최대 재시도 {MAX_AUTO_RETRY}회 도달 — 수동 확인 필요")
            return
        retry_count += 1
        self.history.append(candidate_content)
        self.history.append(types.Content(
            role="user",
            parts=[types.Part(text=_build_repair_prompt(output, retry_count, lang, code))]
        ))
        self.call_from_thread(self._sync_status, retry_count)

    def _refresh_side_tools(self) -> None:
        try:
            panel = self.query_one("#side-panel", SidePanel)
            if panel.display and panel.current_tab == SIDE_TOOLS:
                panel.refresh_tools(self.tool_logs)
        except Exception:
            pass

    @work(thread=True)
    def _call_llm(self, user_text: str):
        enriched_user_text = user_text
        if _is_coding_query(user_text):
            enriched_user_text = f"{user_text}\n\n{_RESPONSE_TEMPLATE}"
        self.history.append(
            types.Content(role="user", parts=[types.Part(text=enriched_user_text)])
        )
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[:2] + self.history[-(MAX_HISTORY - 2):]

        tools = [FS_DECLARATIONS]
        tool_cfg = (types.ToolConfig(includeServerSideToolInvocations=True)
                    if self.model.startswith("gemma") else None)

        retry_count = 0
        format_retry = 0

        try:
            while True:
                cfg = types.GenerateContentConfig(
                    tools=tools,
                    temperature=min(0.75, BASE_TEMPERATURE + retry_count * RETRY_TEMPERATURE_STEP),
                    max_output_tokens=2048,
                )
                if tool_cfg:
                    cfg.tool_config = tool_cfg

                response = self._generate_content_with_key_rotation(
                    model=self.model,
                    contents=self.history,
                    config=cfg,
                )

                if not response.candidates:
                    self.call_from_thread(self._add_message, "error",
                                          "API 응답에 candidates가 없습니다.")
                    break
                candidate = response.candidates[0]
                if not candidate.content or not candidate.content.parts:
                    break
                fn_calls  = [p for p in candidate.content.parts if p.function_call]
                if self.cancel_requested:
                    break

                # ── 도구 호출 처리 (Step 2) ──────────────────────────
                if fn_calls:
                    self.call_from_thread(setattr, self, "loading_message", "도구 실행 결과 반영 중")
                    self.call_from_thread(setattr, self, "loading_detail", "모델이 도구 호출과 응답을 이어서 처리하는 중")
                    self.call_from_thread(self._sync_status, retry_count)
                    tool_results = []
                    for part in fn_calls:
                        fc      = part.function_call
                        fn      = TOOL_FN.get(fc.name)
                        args    = dict(fc.args) if fc.args else {}
                        arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())

                        if fc.name in _APPROVAL_TOOLS:
                            preview = _build_approval_preview(fc.name, args)
                            self.call_from_thread(self._add_message, "system", preview)
                            self._pending_approval = {"name": fc.name}
                            self._approve_event.clear()
                            ok = self._approve_event.wait(timeout=300)
                            approved = ok and self._approve_result
                            self._pending_approval = None
                            if not approved:
                                result = "[거부] 사용자가 실행을 취소했습니다."
                                self.call_from_thread(self._add_message, "retry",
                                                      f"[거부] {fc.name} 취소됨")
                            else:
                                self.call_from_thread(self._add_message, "tool",
                                                      f"[승인] {fc.name}({arg_str})")
                                result = fn(**args) if fn else f"알 수 없는 도구: {fc.name}"
                        else:
                            self.call_from_thread(self._add_message, "tool",
                                                  f"[도구] {fc.name}({arg_str})")
                            result = fn(**args) if fn else f"알 수 없는 도구: {fc.name}"
                        self.tool_logs.append({"name": fc.name, "args": arg_str, "result": result})
                        if len(self.tool_logs) > 500:
                            del self.tool_logs[:-500]
                        self.call_from_thread(self._refresh_side_tools)
                        log_idx = len(self.tool_logs)
                        preview = result[:300] + ("..." if len(result) > 300 else "")
                        summary = f"→ [도구#{log_idx}] {len(result)}자 결과"
                        self.call_from_thread(self._add_message, "tool", summary)
                        if self.tool_detail:
                            self.call_from_thread(self._add_message, "tool", f"→ {preview}")
                        tool_results.append(types.Part(
                            function_response=types.FunctionResponse(
                                name=fc.name, response={"result": result}
                            )
                        ))
                    self.history.append(candidate.content)
                    self.history.append(types.Content(role="user", parts=tool_results))
                    continue

                # ── 최종 텍스트 응답 ─────────────────────────────────
                raw = response.text or ""
                if _is_coding_query(user_text) and not _has_structured_sections(raw) and format_retry < 1:
                    format_retry += 1
                    self.history.append(candidate.content)
                    self.history.append(types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            "응답 형식이 누락되었습니다. 다음 섹션 제목을 반드시 포함해 다시 답하세요: "
                            "계획 / 변경 파일 / 패치 / 검증 명령 / 결과"
                        ))]
                    ))
                    continue

                usage = getattr(response, "usage_metadata", None)
                if usage:
                    self.total_in  += getattr(usage, "prompt_token_count",     0) or 0
                    self.total_out += getattr(usage, "candidates_token_count", 0) or 0

                # ── Step 3: 자동 실행 + 에러 피드백 루프 ────────────
                if self.auto_exec and retry_count < MAX_AUTO_RETRY:
                    blocks = _extract_code_blocks(raw)
                    runnable = [(lang, code) for lang, code in blocks
                                if lang in ("python", "python3", "bash", "sh")
                                and _is_runnable(code)]

                    if runnable:
                        # 우선순위: python > bash
                        runnable.sort(key=lambda x: 0 if x[0] in ("python", "python3") else 1)
                        lang, code = runnable[0]
                        label = f"[시도 {retry_count + 1}/{MAX_AUTO_RETRY}]"
                        self.call_from_thread(setattr, self, "loading_message", f"{lang} 코드 검증 중")
                        self.call_from_thread(setattr, self, "loading_detail", "샌드박스 실행 결과를 확인하는 중")
                        self.call_from_thread(self._add_message, "retry",
                                              f"{label} {lang} 코드 자동 실행 중...")
                        self.call_from_thread(self._sync_status, retry_count + 1)

                        output = _run_in_sandbox(code, lang)

                        # 패키지 누락 시 자동 설치 후 재실행
                        if _is_error(output):
                            pkg = _extract_missing_package(output)
                            if pkg:
                                self.pending_retry_payload = (candidate.content, output, retry_count, lang, code)
                                self.awaiting_pip_confirm = pkg
                                self.call_from_thread(self._add_message, "retry",
                                                      f"필수 패키지 감지: {pkg} — 설치 진행? (y/n)")
                                break

                        self.call_from_thread(self._add_message, "retry",
                                              f"→ 실행 결과:\n{output}")

                        if _is_error(output):
                            self._continue_after_exec_error(candidate.content, output, retry_count, lang, code)
                            retry_count += 1
                            continue
                        else:
                            self.call_from_thread(self._add_message, "retry", "✓ 실행 성공")

                self.history.append(
                    types.Content(role="model", parts=[types.Part(text=raw)])
                )
                if not self.cancel_requested:
                    self.call_from_thread(self._add_message, "assistant", raw)
                break

        except Exception as e:
            kind = _classify_exception_message(e)
            self.call_from_thread(self._add_message, "error", f"[{kind}] {e}")
        finally:
            self.thinking = False
            self.loading_message = "대기 중"
            self.loading_detail = "요청을 기다리는 중"
            self.call_from_thread(self._sync_status, 0)
            self.call_from_thread(lambda: self._input().focus())

    # ── 키 바인딩 액션 ──────────────────────────────────────────────
    def action_new_chat(self):
        self._init_history()
        self.query_one("#chat-scroll", VerticalScroll).remove_children()
        self._add_message("assistant", "새 대화를 시작합니다.")
        self._input().focus()

    def action_save_session(self):
        name = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SESSIONS_DIR / f"{name}.json"
        data = [
            {"role": c.role, "text": c.parts[0].text}
            for c in self.history[2:]
            if c.parts and hasattr(c.parts[0], "text") and c.parts[0].text
        ]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        self._add_message("tool", f"세션 저장 완료: {path}")

    def action_toggle_auto_exec(self):
        self.auto_exec = not self.auto_exec
        self._sync_status()
        self._refresh_input_placeholder()
        state = "ON" if self.auto_exec else "OFF"
        self._add_message("retry", f"자동 실행 {state} — 샌드박스: {SANDBOX_DIR}")

    def action_cancel(self):
        if self.thinking:
            self.cancel_requested = True
            self._add_message("retry", "실행 취소 요청됨 (현재 단계 종료 후 중단)")
        else:
            self._add_message("retry", "취소할 실행이 없습니다.")

    def action_cycle_model(self):
        try:
            idx = AVAILABLE_MODELS.index(self.model)
        except ValueError:
            idx = 0
        self.model = AVAILABLE_MODELS[(idx + 1) % len(AVAILABLE_MODELS)]
        self._sync_status()
        self._add_message("retry", f"모델 변경: {self.model}")

    def action_toggle_side(self) -> None:
        panel = self.query_one("#side-panel", SidePanel)
        panel.display = not panel.display
        if panel.display:
            if panel.current_tab == SIDE_SESSIONS:
                panel.refresh_sessions()
            else:
                panel.refresh_tools(self.tool_logs)

    def action_cycle_side_tab(self) -> None:
        panel = self.query_one("#side-panel", SidePanel)
        if not panel.display:
            panel.display = True
        panel.cycle_tab()
        if panel.current_tab == SIDE_SESSIONS:
            panel.refresh_sessions()
        else:
            panel.refresh_tools(self.tool_logs)

    def action_quit(self):
        self.exit()


if __name__ == "__main__":
    TuiCoder().run()
