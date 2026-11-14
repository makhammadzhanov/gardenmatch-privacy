#!/usr/bin/env python3
"""Проверка формата сообщений коммитов (только стандартная библиотека).

Режимы:
  <файл>              одно сообщение (вызов из .githooks/commit-msg):
                      строки на '#' и всё после '# ------------------------ >8'
                      отбрасываются, пробелы в конце строк и пустые строки в
                      конце сообщения убираются;
  --range <A..B>      коммиты диапазона из git log (принимается любой rev-list);
  --all               вся история текущей ветки;
  --messages <файл>   пакет сообщений '=== <sha>' + текст; дополнительно
                      сверяется, что список sha и порядок совпадают с
                      'git rev-list --reverse <base>' (по умолчанию c8b4e41);
  --self-test         встроенные проверки хороших и плохих сообщений.

Формат: 'type(scope): description', пустая строка, затем 1..N пунктов '- ...'.
Код выхода 0 — ошибок нет, 1 — есть (или сломан сам вызов).
"""

import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ALLOWED_TYPES = (
    "feat",
    "fix",
    "refactor",
    "core",
    "docs",
    "test",
    "build",
    "chore",
    "perf",
    "revert",
)
SCOPE_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*\Z")
HEADER_RE = re.compile(r"([a-z]+)\(([^()]*)\): (.+)\Z", re.S)
DESCRIPTION_START_RE = re.compile(r"[a-z0-9]")

HEADER_LIMIT = 72
BODY_LIMIT = 100
CUT_MARKER = "# ------------------------ >8"

# Трейлеры и упоминания автора-инструмента запрещены целиком (регистр не важен).
FORBIDDEN_MARKERS = (
    "co-authored-by",
    "signed-off-by",
    "claude-session",
    "generated with",
    "claude",
    "codebuff",
)

# База для режима --messages: коммит, перед которым начинается перезапись.
DEFAULT_BASE = "HEAD"


def strip_message(text, drop_comments=False):
    """Убрать комментарии git (только режим хука), пробелы в конце строк и хвост."""
    lines = text.splitlines()
    kept = []
    for line in lines:
        if drop_comments:
            if line.startswith(CUT_MARKER):
                break
            if line.startswith("#"):
                continue
        kept.append(line.rstrip())
    while kept and not kept[-1]:
        kept.pop()
    return kept


def _printable(line):
    return all(0x20 <= ord(ch) <= 0x7E for ch in line)


def validate(lines):
    """Вернуть список причин, по которым сообщение не годится (пусто — годится)."""
    errors = []

    if not lines:
        return ["пустое сообщение"]

    for number, line in enumerate(lines, 1):
        if not _printable(line):
            bad = sorted({ch for ch in line if not 0x20 <= ord(ch) <= 0x7E})
            errors.append(
                "line %d: only printable ASCII 0x20-0x7E is allowed, found %r"
                % (number, "".join(bad))
            )

    header = lines[0]
    if len(header) > HEADER_LIMIT:
        errors.append(
            "line 1: %d symbols, limit is %d" % (len(header), HEADER_LIMIT)
        )

    match = HEADER_RE.match(header)
    if not match:
        errors.append("line 1: expected 'type(scope): description'")
    else:
        ctype, scope, description = match.groups()
        if ctype not in ALLOWED_TYPES:
            errors.append(
                "line 1: unknown type %r (allowed: %s)"
                % (ctype, ", ".join(ALLOWED_TYPES))
            )
        if not scope:
            errors.append("line 1: scope is required, syntax 'type(scope): ...'")
        elif not SCOPE_RE.match(scope):
            errors.append(
                "line 1: scope %r must match [a-z0-9][a-z0-9._/-]*" % scope
            )
        if not DESCRIPTION_START_RE.match(description):
            errors.append(
                "line 1: description must start with a lowercase letter or digit"
            )
        if description.endswith("."):
            errors.append("line 1: description must not end with a period")

    if len(lines) < 2 or lines[1]:
        errors.append("line 2: must be empty (blank line after the header)")

    body = lines[2:]
    if not body:
        errors.append("body: at least one '- ' item is required")
    for number, line in enumerate(body, 3):
        if not line:
            errors.append("line %d: empty line in the body" % number)
        elif not line.startswith("- "):
            errors.append("line %d: item must start with '- '" % number)
        elif not line[2:].strip():
            errors.append("line %d: empty item text" % number)
        if len(line) > BODY_LIMIT:
            errors.append(
                "line %d: %d symbols, limit is %d"
                % (number, len(line), BODY_LIMIT)
            )

    for number, line in enumerate(lines, 1):
        lowered = line.lower()
        for marker in FORBIDDEN_MARKERS:
            if marker in lowered:
                errors.append(
                    "line %d: forbidden trailer or author mention %r"
                    % (number, line.strip())
                )
                break

    return errors


def git_text(*args):
    proc = subprocess.run(
        ["git"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if proc.returncode != 0:
        sys.stderr.write(
            "git %s: %s\n"
            % (" ".join(args), proc.stderr.decode("utf-8", "replace").strip())
        )
        raise SystemExit(1)
    return proc.stdout.decode("utf-8", "replace")


def read_commits(rev_args):
    """[(sha, текст сообщения)] в порядке rev-list."""
    raw = git_text("log", "--reverse", "--format=%x1e%H%n%B", *rev_args)
    commits = []
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        sha, _, body = chunk.partition("\n")
        commits.append((sha.strip(), body))
    return commits


def read_file(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def report(label, lines):
    errors = validate(lines)
    for error in errors:
        print("%s: %s" % (label, error))
    return errors


def parse_messages(text):
    """Разобрать пакет '=== <sha>' + сообщение в список блоков."""
    blocks = []
    current = None
    stray = []
    for line in text.splitlines():
        if line.startswith("=== "):
            current = {"sha": line[4:].strip(), "lines": []}
            blocks.append(current)
        elif current is None:
            if line.strip():
                stray.append(line)
        else:
            current["lines"].append(line)
    return blocks, stray


def check_messages_file(path, base):
    try:
        text = read_file(path)
    except OSError as error:
        print("%s: cannot read: %s" % (path, error))
        return 1

    blocks, stray = parse_messages(text)
    failed = 0

    for line in stray:
        print("%s: text before the first '=== <sha>' block: %r" % (path, line))
        failed += 1

    expected = [sha for sha, _ in read_commits([base]) if sha]
    actual = [block["sha"] for block in blocks]

    if len(actual) != len(expected):
        print(
            "%s: %d blocks, git rev-list --reverse %s gives %d commits"
            % (path, len(actual), base, len(expected))
        )
        failed += 1

    for index in range(max(len(actual), len(expected))):
        want = expected[index] if index < len(expected) else "<none>"
        got = actual[index] if index < len(actual) else "<none>"
        if want != got:
            print(
                "%s: block %d: expected %s, got %s (order must match rev-list)"
                % (path, index + 1, want, got)
            )
            failed += 1

    for block in blocks:
        sha = block["sha"]
        if not re.match(r"[0-9a-f]{40}\Z", sha):
            print("%s: block %r: sha must be 40 lowercase hex symbols" % (path, sha))
            failed += 1
            continue
        lines = strip_message("\n".join(block["lines"]))
        if report(sha, lines):
            failed += 1

    if failed:
        print("%s: %d error(s)" % (path, failed))
        return 1

    print("%s: %d messages ok" % (path, len(blocks)))
    return 0


SELF_TEST_CASES = [
    (
        "good message",
        "fix(hud): keep health bar within screen bounds\n"
        "\n"
        "- clamp bar position to the viewport\n"
        "- log a warning when the bar is off-screen\n",
        True,
    ),
    (
        "cyrillic description",
        "fix(hud): \u043f\u043e\u0447\u0438\u043d\u0438\u0442\u044c \u043f\u043e\u043b\u043e\u0441\u0443 \u0437\u0434\u043e\u0440\u043e\u0432\u044c\u044f\n\n- keep the bar inside the viewport\n",
        False,
    ),
    (
        "cyrillic body",
        "fix(hud): keep health bar within screen bounds\n\n- \u043f\u043e\u0447\u0438\u043d\u0438\u0442\u044c \u043f\u043e\u043b\u043e\u0441\u0443\n",
        False,
    ),
    (
        "co-authored-by trailer",
        "fix(hud): keep health bar within screen bounds\n\n- clamp bar position\n\nCo-Authored-By: Someone <a@b.c>\n",
        False,
    ),
    (
        "generated with trailer",
        "fix(hud): keep health bar within screen bounds\n\n- clamp bar position\n\nGenerated with Tool\n",
        False,
    ),
    (
        "claude mention",
        "docs(plan): describe claude workflow\n\n- mention the workflow\n",
        False,
    ),
    (
        "codebuff mention",
        "docs(plan): describe tool authors\n\n- mention Codebuff as a helper\n",
        False,
    ),
    (
        "no body",
        "fix(hud): keep health bar within screen bounds\n",
        False,
    ),
    (
        "header without blank line",
        "fix(hud): keep health bar within screen bounds\n- clamp bar position\n",
        False,
    ),
    (
        "unknown type",
        "wip(hud): keep health bar within screen bounds\n\n- clamp bar position\n",
        False,
    ),
    (
        "missing scope",
        "fix: keep health bar within screen bounds\n\n- clamp bar position\n",
        False,
    ),
    (
        "bad scope",
        "fix(HUD): keep health bar within screen bounds\n\n- clamp bar position\n",
        False,
    ),
    (
        "capitalized description",
        "fix(hud): Keep health bar within screen bounds\n\n- clamp bar position\n",
        False,
    ),
    (
        "description with period",
        "fix(hud): keep health bar within screen bounds.\n\n- clamp bar position\n",
        False,
    ),
    (
        "header too long",
        "fix(hud): " + "x" * 70 + "\n\n- clamp bar position\n",
        False,
    ),
    (
        "body item without dash",
        "fix(hud): keep health bar within screen bounds\n\nclamp bar position\n",
        False,
    ),
    (
        "empty line in body",
        "fix(hud): keep health bar within screen bounds\n\n- clamp bar position\n\n- log a warning\n",
        False,
    ),
    (
        "body item too long",
        "fix(hud): keep health bar within screen bounds\n\n- " + "x" * 99 + "\n",
        False,
    ),
    (
        "empty body item",
        "fix(hud): keep health bar within screen bounds\n\n-   \n",
        False,
    ),
    (
        "emoji in body",
        "fix(hud): keep health bar within screen bounds\n\n- clamp bar position \U0001f600\n",
        False,
    ),
    (
        "many items",
        "feat(core): add hook registry\n\n- add registry\n- add lookup\n- add tests\n",
        True,
    ),
]


def self_test():
    failed = 0
    for name, text, should_pass in SELF_TEST_CASES:
        errors = validate(strip_message(text))
        ok = not errors
        if ok != should_pass:
            failed += 1
            print(
                "self-test: %s: expected %s, got %s (%s)"
                % (
                    name,
                    "pass" if should_pass else "reject",
                    "pass" if ok else "reject",
                    "; ".join(errors) or "no errors",
                )
            )
    # Режим хука: комментарии git и строка-отсечка не считаются сообщением.
    hook_text = (
        "fix(hud): keep health bar within screen bounds\n"
        "\n"
        "- clamp bar position\n"
        "\n"
        "# Please enter the commit message for your changes.\n"
        "# Lines starting\n"
        "# ------------------------ >8 ------------------------\n"
        "# Do not modify or remove the line above.\n"
        "\u0434\u0438\u0444\u0444\n"
    )
    if validate(strip_message(hook_text, drop_comments=True)):
        failed += 1
        print("self-test: hook comment stripping: expected pass, got reject")
    if not validate(strip_message(hook_text)):
        failed += 1
        print("self-test: comments must be rejected without drop_comments")

    if failed:
        print("self-test: %d failure(s)" % failed)
        return 1
    print("self-test: %d cases ok" % (len(SELF_TEST_CASES) + 2))
    return 0


def main(argv):
    path = None
    rev_range = None
    all_commits = False
    messages = None
    base = DEFAULT_BASE
    self_test_mode = False

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--self-test":
            self_test_mode = True
        elif arg == "--all":
            all_commits = True
        elif arg in ("--range", "--messages", "--base"):
            if index + 1 >= len(argv):
                sys.stderr.write("check_commit_msg: %s needs a value\n" % arg)
                return 1
            value = argv[index + 1]
            index += 1
            if arg == "--range":
                rev_range = value
            elif arg == "--messages":
                messages = value
            else:
                base = value
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        elif arg.startswith("-"):
            sys.stderr.write("check_commit_msg: unknown option %s\n" % arg)
            return 1
        elif path is None:
            path = arg
        else:
            sys.stderr.write("check_commit_msg: unexpected argument %s\n" % arg)
            return 1
        index += 1

    modes = [
        bool(path),
        bool(rev_range),
        all_commits,
        bool(messages),
        self_test_mode,
    ]
    if sum(modes) != 1:
        sys.stderr.write(
            "check_commit_msg: use exactly one of <file>, --range, --all, "
            "--messages, --self-test\n"
        )
        return 1

    if self_test_mode:
        return self_test()

    if messages:
        return check_messages_file(messages, base)

    if path:
        try:
            text = read_file(path)
        except OSError as error:
            print("%s: cannot read: %s" % (path, error))
            return 1
        lines = strip_message(text, drop_comments=True)
        if report(path, lines):
            return 1
        print("%s: message ok" % path)
        return 0

    if rev_range:
        rev_args = [rev_range]
    else:
        # --all: вся история текущей ветки, а не все ссылки (origin включительно).
        rev_args = ["HEAD"]

    commits = read_commits(rev_args)
    failed = 0
    for sha, body in commits:
        if report(sha, strip_message(body)):
            failed += 1
    if failed:
        print("%d of %d commits have invalid messages" % (failed, len(commits)))
        return 1
    print("%d commits ok" % len(commits))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
