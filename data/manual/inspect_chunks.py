# data/manual/inspect_chunks.py
import gzip, json
from pathlib import Path

# 스크립트 파일이 있는 폴더를 기준으로 경로 설정
HERE = Path(__file__).parent
CHUNKS_GZ = HERE / "_chunks.jsonl.gz"


def main():
    print(f"🔍 청크 파일 검사: {CHUNKS_GZ}\n")
    if not CHUNKS_GZ.exists():
        print("❌ _chunks.jsonl.gz를 찾을 수 없습니다. build_index.py를 먼저 실행해 주세요.")
        return

    with gzip.open(CHUNKS_GZ, "rt", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if i > 20:
                break
            chunk = json.loads(line)["text"]
            preview = chunk.replace("\n", " ")[:300]
            print(f"--- CHUNK #{i} (길이 {len(chunk)}자) ---")
            print(preview)
            print()
    print("🔍 검사 완료")


if __name__ == "__main__":
    main()
