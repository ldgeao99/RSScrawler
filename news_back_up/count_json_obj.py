import json
from pathlib import Path

for file in sorted(Path(".").glob("*.json")):
    try:
        with open(file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            print(f"{file.name}: {len(data):,}개")
        else:
            print(f"{file.name}: 최상위가 리스트가 아닙니다. (type={type(data).__name__})")

    except Exception as e:
        print(f"{file.name}: 오류 - {e}")