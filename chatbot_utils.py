# chatbot_utils.py

from sentence_transformers import SentenceTransformer, util
import json
import torch

# ✅ 1. FAQ 템플릿 로딩
with open("faq_templates.json", "r", encoding="utf-8") as f:
    faq_data = json.load(f)

questions = [item["question"] for item in faq_data]
answers   = [item["answer"]   for item in faq_data]

# ✅ 2. 매뉴얼 JSON 로딩
with open("모든_방재매뉴얼_데이터.json", "r", encoding="utf-8") as f:
    manuals = json.load(f)

# 매뉴얼 섹션을 리스트로 평탄화
manual_sections = []
for m in manuals:
    for sec in m.get("sections", []):
        manual_sections.append({
            "title":   sec["section_title"],
            "content": sec["content"]
        })

# ✅ 3. 사전 학습된 한국어 멀티태스크 Sentence-BERT 로드
model = SentenceTransformer("jhgan/ko-sroberta-multitask")

# ✅ 4. 모든 FAQ 질문 임베딩 벡터화 (Tensor)
question_embeddings = model.encode(questions, convert_to_tensor=True)

# ⚙️ 유사도 Threshold 설정
SIM_THRESHOLD = 0.6  # 0.5~0.7 사이로 조정하세요

def get_best_match(user_query):
    # 1) FAQ 유사도 계산
    q_emb    = model.encode(user_query, convert_to_tensor=True)
    sims     = util.pytorch_cos_sim(q_emb, question_embeddings)[0]
    best_idx = torch.argmax(sims).item()
    best_score = sims[best_idx].item()

    # 2) FAQ 매칭 성립 시
    if best_score >= SIM_THRESHOLD:
        return {
            "question": questions[best_idx],
            "answer":   answers[best_idx],
            "score":    round(best_score, 3)
        }

    # 3) FAQ 매칭 실패 시 매뉴얼 섹션 키워드 검색
    #    (간단하게 제목/내용에 키워드 포함 여부로 확인)
    query_norm = user_query.replace(" ", "").lower()
    for sec in manual_sections:
        title_norm = sec["title"].replace(" ", "").lower()
        if title_norm in query_norm or query_norm in title_norm:
            return {
                "question": sec["title"],
                "answer":   sec["content"],
                "score":    None
            }

    # 4) 모두 실패 시 Fallback
    return {
        "question": None,
        "answer":   "죄송합니다. 해당 내용을 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요.",
        "score":    round(best_score, 3)
    }
