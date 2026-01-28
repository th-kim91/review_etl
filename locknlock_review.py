# app.py
# Streamlit: JD(징동) / Tmall(티몰) 리뷰 텍스트 복붙 → (작성자id, 작성일자, 리뷰내용) 표 변환 + CSV 다운로드
# - JD:
#   ✅ avatar/author/star/date/product/review 패턴 지원
#   ✅ '01-13' 처럼 연도 없는 날짜는 default_year(기본 2026) 붙여 '2026-01-13'으로 변환
#   ✅ pic 뒤에 이어지는 追评 텍스트도 리뷰내용에 포함 (pic은 종료 신호가 아님)
#   ✅ 商家回复: ... 는 리뷰내용에서 제외
#   ✅ 입력에 상단/하단 "京东首页..." 같은 페이지 UI가 섞여 들어오는 경우 자동 제거
# - Tmall: 기존 로직 유지 (YYYY年M月D日已购：... + 추평)
# - 중복 제거(수정):
#   ✅ 중복키 = (작성자id, 작성일자, 리뷰 첫 단어)
#   ✅ 중복이면 "두 번째" 행만 남김 (1개면 그대로)

import re
import pandas as pd
import streamlit as st
from typing import List, Optional

# ---------------------------
# 공통 유틸
# ---------------------------
def _normalize_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = line.replace("￼", "").strip()
        if not line:
            continue
        lines.append(line)
    return lines

def _is_noise_line_common(line: str) -> bool:
    if re.fullmatch(r"\d+", line):
        return True
    if re.fullmatch(r"[•·\-\—_]+", line):
        return True
    return False

# ✅ 추가: 리뷰 첫 단어 추출
def _first_word(text: str) -> str:
    if not text:
        return ""
    parts = str(text).strip().split()
    return parts[0] if parts else ""

# ✅ 추가: (id, date, first_word) 기준 중복 제거 + 두 번째 유지
def _dedupe_keep_second_by_firstword(df: pd.DataFrame) -> pd.DataFrame:
    """
    중복키 = (작성자id, 작성일자, 리뷰 첫 단어)
    - 그룹 size == 1: 1번째(유일한) 행 유지
    - 그룹 size >= 2: 2번째 행만 유지 (그 이후 3번째~는 버림)
    """
    if df.empty:
        return df

    df = df.copy()
    df["_first_word"] = df["리뷰내용"].astype(str).apply(_first_word)

    # 그룹 내 순번(0,1,2...) 부여
    df["_rank"] = df.groupby(["작성자id", "작성일자", "_first_word"]).cumcount()
    # 그룹 크기
    df["_gsize"] = df.groupby(["작성자id", "작성일자", "_first_word"])["_rank"].transform("size")

    keep_mask = ((df["_gsize"] == 1) & (df["_rank"] == 0)) | ((df["_gsize"] >= 2) & (df["_rank"] == 1))
    df = df.loc[keep_mask].reset_index(drop=True)

    return df.drop(columns=["_first_word", "_rank", "_gsize"])

# ---------------------------
# JD 입력 전처리 (핵심!)
# ---------------------------
def _preclean_jd_text(raw_text: str) -> str:
    """
    JD 복붙 텍스트는 페이지 상단/하단 UI가 같이 들어오는 경우가 많아서,
    파싱 전에 아래를 수행:
    1) 첫 'avatar' 이전 구간은 버림 (상단 UI 제거)
    2) '京东首页' 같은 헤더가 "다시" 등장하면(2번째 등장) 그 지점부터 끝까지 버림 (하단 UI 제거)
       - 네 input처럼 위/아래에 모두 '京东首页'가 있을 때 특히 효과적
    """
    lines = _normalize_lines(raw_text)
    if not lines:
        return ""

    # 1) 첫 avatar 이전 삭제
    try:
        first_avatar_idx = lines.index("avatar")
        lines = lines[first_avatar_idx:]
    except ValueError:
        # avatar 자체가 없으면 그대로 반환
        return "\n".join(lines)

    # 2) '京东首页'가 2번 이상 나오면 2번째부터 끝 삭제
    header_key = "京东首页"
    hit = 0
    cut_idx = None
    for idx, line in enumerate(lines):
        if header_key in line:
            hit += 1
            if hit >= 2:
                cut_idx = idx
                break
    if cut_idx is not None:
        lines = lines[:cut_idx]

    return "\n".join(lines)

# ---------------------------
# JD (징동) 파서
# ---------------------------
JD_DATE_FULL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
JD_DATE_SHORT_RE = re.compile(r"^\d{2}-\d{2}$")

def parse_jd(text: str, default_year: int = 2026) -> pd.DataFrame:
    # ✅ 전처리 먼저
    text = _preclean_jd_text(text)
    lines = _normalize_lines(text)

    MARKER_START = "avatar"
    NOISE_TOKENS = {"pic", "more", "star", "回复", "有用"}

    def is_noise_jd(line: str) -> bool:
        if _is_noise_line_common(line):
            return True
        if line in {MARKER_START, *NOISE_TOKENS}:
            return True
        return False

    def normalize_jd_date(line: str) -> Optional[str]:
        if JD_DATE_FULL_RE.match(line):
            return line
        if JD_DATE_SHORT_RE.match(line):
            return f"{default_year}-{line}"
        return None

    rows = []
    i = 0

    while i < len(lines):
        if lines[i] != MARKER_START:
            i += 1
            continue

        # author
        if i + 1 >= len(lines):
            break
        author = lines[i + 1].strip()

        # star 찾기
        j = i + 2
        while j < len(lines) and lines[j] != "star" and lines[j] != MARKER_START:
            j += 1
        if j >= len(lines) or lines[j] == MARKER_START:
            i = j
            continue

        # date 찾기 (star 다음쪽에서)
        j += 1
        date = None
        date_raw = None
        steps = 0
        while j < len(lines) and steps < 40:
            if lines[j] == MARKER_START:
                break
            nd = normalize_jd_date(lines[j])
            if nd:
                date = nd
                date_raw = lines[j]
                break
            j += 1
            steps += 1
        if not date:
            i = j
            continue

        # product: date 다음 1줄(옵션/상품명)일 수도 있음
        j += 1
        product = None
        if j < len(lines) and lines[j] != MARKER_START and not is_noise_jd(lines[j]):
            product = lines[j]
            j += 1

        # 리뷰 본문: 다음 avatar 전까지
        content_lines = []
        while j < len(lines):
            cur = lines[j]
            if cur == MARKER_START:
                break

            if cur.startswith("商家回复"):
                j += 1
                continue

            # 중복 방지
            if cur == author:
                j += 1
                continue
            if date_raw and cur == date_raw:
                j += 1
                continue
            if product and cur == product:
                j += 1
                continue

            if not is_noise_jd(cur):
                content_lines.append(cur)

            j += 1

        review = " ".join(content_lines).strip()
        if not review:
            review = "내용 없음"

        rows.append({"작성자id": author, "작성일자": date, "리뷰내용": review})
        i = j

    df = pd.DataFrame(rows, columns=["작성자id", "작성일자", "리뷰내용"])

    # ✅ 여기만 변경: 중복 제거 로직 교체
    df = _dedupe_keep_second_by_firstword(df)

    return df

# ---------------------------
# Tmall (티몰) 파서 (기존 유지)
# ---------------------------
TMALL_PURCHASE_LINE_RE = re.compile(r"^(20\d{2})年(\d{1,2})月(\d{1,2})日已购：(.+)$")
TMALL_APPEND_RE = re.compile(r"^\d+天后追评：(.+)$")

def parse_tmall(text: str) -> pd.DataFrame:
    lines = _normalize_lines(text)

    def is_noise_tmall(line: str) -> bool:
        noise_phrases = [
            "为你展示真实评价", "默认排序", "款式筛选", "更多",
            "有用", "回复",
        ]
        if any(p in line for p in noise_phrases):
            return True
        if _is_noise_line_common(line):
            return True
        if line.startswith("商家回复"):
            return True
        return False

    def normalize_date(y: str, m: str, d: str) -> str:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

    def looks_like_author_id(s: str) -> bool:
        if " " in s:
            return False
        if len(s) < 2 or len(s) > 60:
            return False
        if TMALL_PURCHASE_LINE_RE.match(s):
            return False
        if TMALL_APPEND_RE.match(s):
            return False
        if any(x in s for x in ["展示", "排序", "筛选", "已购"]):
            return False
        return True

    def is_start_of_next_review(idx: int) -> bool:
        if not looks_like_author_id(lines[idx]):
            return False
        steps = 0
        j = idx + 1
        while j < len(lines) and steps < 8:
            if is_noise_tmall(lines[j]):
                j += 1
                steps += 1
                continue
            if TMALL_PURCHASE_LINE_RE.match(lines[j]):
                return True
            return False
        return False

    rows = []
    i = 0

    while i < len(lines):
        m = TMALL_PURCHASE_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue

        y, mo, d, _ = m.groups()
        date = normalize_date(y, mo, d)

        author = "UNKNOWN"
        for back in [1, 2, 3, 4, 5, 6]:
            if i - back >= 0:
                cand = lines[i - back]
                if looks_like_author_id(cand) and not is_noise_tmall(cand):
                    author = cand
                    break

        i += 1
        main_parts = []
        append_parts = []

        while i < len(lines):
            line = lines[i]
            if TMALL_PURCHASE_LINE_RE.match(line):
                break
            if is_start_of_next_review(i):
                break

            if line.startswith("商家回复"):
                i += 1
                continue

            am = TMALL_APPEND_RE.match(line)
            if am:
                txt = am.group(1).strip()
                if txt:
                    append_parts.append(txt)
                i += 1
                continue

            if not is_noise_tmall(line):
                main_parts.append(line)

            i += 1

        main_text = " ".join(main_parts).strip()
        append_text = " ".join(append_parts).strip()

        if append_text and main_text == "该用户未填写评价内容":
            main_text = ""

        if main_text and append_text:
            review = f"{main_text} 追评: {append_text}"
        elif append_text:
            review = f"追评: {append_text}"
        elif main_text:
            review = main_text
        else:
            review = "내용 없음"

        rows.append({"작성자id": author, "작성일자": date, "리뷰내용": review})

    df = pd.DataFrame(rows, columns=["작성자id", "작성일자", "리뷰내용"])

    # ✅ 여기만 변경: 중복 제거 로직 교체
    df = _dedupe_keep_second_by_firstword(df)

    return df

# ---------------------------
# Streamlit UI
# ---------------------------
st.set_page_config(page_title="JD/Tmall 리뷰 파서", layout="wide")
st.title("JD / Tmall 리뷰 텍스트 → 표 변환기")
st.write("리뷰 텍스트를 복붙하면 **작성자id / 작성일자 / 리뷰내용**으로 파싱해서 표로 보여주고 CSV로 다운로드합니다.")

platform = st.radio("플랫폼 선택", ["징동 (JD)", "티몰 (Tmall)"], horizontal=True)

default_jd = ""
default_tmall = """N**1
2025年3月12日已购：框架结构 / 1500mm*2000mm / Elephant床/进口布艺/进口华夫格纯棉全拆床垫

萱**麻
2025年3月12日已购：框架结构 / 1800mm*2000mm / Elephant床/进口布艺/单床
0
11
实物与图片一致美感十足，家具做工十分精致
"""

text = st.text_area(
    "리뷰 원문 텍스트",
    value=default_jd if platform.startswith("징동") else default_tmall,
    height=380
)

col1, col2 = st.columns([1, 1])
with col1:
    do_parse = st.button("파싱 실행", type="primary")
with col2:
    show_raw_lines = st.checkbox("디버그: 정리된 라인 보기", value=False)

if do_parse:
    if platform.startswith("징동"):
        df = parse_jd(text, default_year=2026)
    else:
        df = parse_tmall(text)

    st.subheader("파싱 결과")
    st.caption(f"총 {len(df)}건")
    st.dataframe(df, use_container_width=True)

    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "CSV 다운로드 (utf-8-sig)",
        data=csv_bytes,
        file_name="reviews_parsed.csv",
        mime="text/csv",
    )

    if show_raw_lines:
        st.subheader("디버그: 정리된 라인")
        st.code("\n".join(_normalize_lines(_preclean_jd_text(text))))
