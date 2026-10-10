"""Reject private files, staff addresses and recognizable secrets in Git.

Only indexed files are inspected. Local credentials and operational notes
remain usable when ignored; the check never prints their contents.
"""

from pathlib import Path
import re
import subprocess
import sys
import zipfile


root = Path(__file__).resolve().parents[1]
paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
email = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
slack_user = re.compile(r"\bU0[A-Z0-9]{8,19}\b")
secret = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|xox[baprs]-[A-Za-z0-9-]{20,}|apify_api_[A-Za-z0-9]{25,}"
    r"|sk-(?:proj-)?[A-Za-z0-9_-]{32,}"
    r"|postgres(?:ql)?://[^\s/:]+:[^\s@/]+@"
)
violations = []


def private_path(name):
    path = Path(name)
    return (
        path.name.startswith(("FOR_CODEX", "AGENTS")) and path.suffix == ".md"
        or name.startswith(("src/data/", "data/", "Post DB/"))
        or "firebase-adminsdk" in path.name
        or path.name == "sentient-roster.json"
        or path.suffix in {".sqlite", ".sqlite3", ".db", ".pem", ".key"}
        or path.name.startswith(".env") and ".example" not in path.name
    )


def inspect(name, payload):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return
    for number, line in enumerate(text.splitlines(), 1):
        if secret.search(line):
            violations.append(f"{name}:{number}: credential-shaped value")
        if any(not value.startswith(("U0TEST", "U0MOCK", "U012345")) for value in slack_user.findall(line)):
            violations.append(f"{name}:{number}: use a synthetic Slack user ID in public code")
        if re.search(r"https://(?:avatars|ca)\.slack-edge\.com/[A-Za-z0-9/_.-]{20,}", line):
            violations.append(f"{name}:{number}: private staff avatar URL")
        for match in email.finditer(line):
            # URL userinfo fixtures are not staff email addresses.
            prefix = line[:match.start()]
            if re.search(r"https?://(?:[^/@\s\"']*:)?$", prefix):
                continue
            if match[0] in {"system@sentientdash.app", "queue-system@sentientdash.app", "queue-retention@sentientdash.app"}:
                continue
            domain = match[1].lower()
            if domain in {"example.com", "example.org", "example.net", "localhost.localdomain"} or domain.endswith((".example", ".test", ".invalid")):
                continue
            violations.append(f"{name}:{number}: use an example address in public code")
            break


for name in filter(None, paths):
    if private_path(name):
        violations.append(f"{name}: private file is tracked")
        continue
    path = root / name
    if not path.is_file():
        continue
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                member_name = f"{name}:{member.filename}"
                if private_path(member.filename):
                    violations.append(f"{member_name}: private file in public archive")
                elif not member.is_dir():
                    inspect(member_name, archive.read(member))
    else:
        inspect(name, path.read_bytes())

if violations:
    print("Public-repository check failed:\n" + "\n".join(violations), file=sys.stderr)
    sys.exit(1)
print("Public-repository check passed: no private files, staff addresses or recognizable secrets.")
