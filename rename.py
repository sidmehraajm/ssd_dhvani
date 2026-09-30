from pathlib import Path

for p in Path(".").rglob("*"):
    if p.suffix in {".py", ".bat", ".spec", ".md", ".txt", ".qss", ".json"} \
            and ".venv" not in p.parts:
        t = p.read_text(encoding="utf-8")
        if "ssd_dhvani" in t:
            p.write_text(t.replace("ssd_dhvani", "ssd_dhvani"), encoding="utf-8")
            print("updated", p)