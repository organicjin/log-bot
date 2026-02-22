import os
import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from notion_client import AsyncClient as NotionClient
import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

load_dotenv()

# ─── 환경변수 ──────────────────────────────────────────
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
NOTION_API_KEY = (os.getenv("NOTION_API_KEY") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

NOTION_DB_IDS = {
    "health": (os.getenv("NOTION_HEALTH_DB_ID") or "").strip(),
    "discuss": (os.getenv("NOTION_DISCUSSION_DB_ID") or "").strip(),
    "read": (os.getenv("NOTION_READING_DB_ID") or "").strip(),
    "growth": (os.getenv("NOTION_GROWTH_DB_ID") or "").strip(),
    "review": (os.getenv("NOTION_REVIEW_DB_ID") or "").strip(),
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
DAY_NAMES = ["월", "화", "수", "목", "금", "토", "일"]

sessions: dict[str, dict] = {}
chat_ids: set[int] = set()  # 봇에 메시지 오면 자동 저장
waiting_for_comment: dict[int, str] = {}  # chat_id → notion_page_id (한 줄 소감 대기)


# ─── Select 필드 유효값 ──────────────────────────────────
VALID_SELECTS = {
    "컨디션": ["최고", "좋음", "보통", "피곤", "아픔"],
    "수면": ["충분", "보통", "부족"],
    "영양제": ["먹음", "안먹음"],
    "감정 상태": ["평온", "감사", "설렘", "뿌듯", "보통", "불안", "우울", "짜증", "외로움", "지침", "혼란", "복잡"],
    "컨디션 종합": ["좋음", "보통", "힘들었음"],
}


# ─── 유틸 ──────────────────────────────────────────
def today_title() -> str:
    now = datetime.now(KST)
    return f"{now.month}/{now.day}({DAY_NAMES[now.weekday()]})"


def today_iso() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _trunc(s: str, max_len: int = 300) -> str:
    if not s:
        return "-"
    return s[:max_len] + "..." if len(s) > max_len else s


def validate_select(value: str, field: str, default: str = "보통") -> str:
    valid = VALID_SELECTS.get(field, [])
    return value if value in valid else default


def validate_multi_select(values: list, field: str) -> list:
    valid = VALID_SELECTS.get(field, [])
    return [v for v in values if v in valid] if valid else values


# ─── Claude 추출 프롬프트 ──────────────────────────────
EXTRACT_PROMPTS = {
    "health": """다음 텍스트에서 건강 기록 정보를 추출해서 JSON만 응답하세요. 다른 텍스트는 절대 넣지 마세요.

텍스트:
{text}

응답 형식:
{{
  "아침": "아침 식사 내용 (없으면 빈 문자열)",
  "점심": "점심 식사 내용 (없으면 빈 문자열)",
  "저녁": "저녁 식사 내용 (없으면 빈 문자열)",
  "간식": "간식 내용 (없으면 빈 문자열)",
  "운동": "운동 내용 (없으면 빈 문자열)",
  "컨디션": "최고/좋음/보통/피곤/아픔 중 정확히 하나",
  "수면": "충분/보통/부족 중 정확히 하나",
  "영양제": "먹음/안먹음 중 정확히 하나",
  "오늘 잘한 것": "잘한 점 (없으면 빈 문자열)",
  "메모": "기타 특이사항 (없으면 빈 문자열)"
}}""",

    "discuss": """다음 텍스트에서 콘텐츠 토론 정보를 추출해서 JSON만 응답하세요. 다른 텍스트는 절대 넣지 마세요.

텍스트:
{text}

응답 형식:
{{
  "제목": "토론 주제를 나타내는 간결한 제목 (최대 80자)",
  "원본URL": "원본 콘텐츠 URL (텍스트에 URL이 있으면 추출, 없으면 빈 문자열)",
  "핵심 인사이트": "핵심 인사이트 요약",
  "내 삶 적용 포인트": "실생활에 적용할 수 있는 포인트",
  "태그": ["태그1", "태그2", "태그3"]
}}""",

    "read": """다음 텍스트에서 독서 기록 정보를 추출해서 JSON만 응답하세요. 다른 텍스트는 절대 넣지 마세요.

텍스트:
{text}

응답 형식:
{{
  "제목": "독서 기록 제목 (최대 80자)",
  "책 이름": "책 제목",
  "챕터/페이지": "읽은 챕터나 페이지 범위 (없으면 빈 문자열)",
  "핵심 요약": "읽은 내용의 핵심 요약",
  "내 삶 적용 포인트": "실생활에 적용할 수 있는 포인트",
  "인상 깊은 문장": "인상 깊은 문장이나 구절 (없으면 빈 문자열)"
}}""",

    "growth": """다음 텍스트에서 내면 성장 기록 정보를 추출해서 JSON만 응답하세요. 다른 텍스트는 절대 넣지 마세요.

텍스트:
{text}

감정 상태는 반드시 다음 목록에서만 선택: 평온, 감사, 설렘, 뿌듯, 보통, 불안, 우울, 짜증, 외로움, 지침, 혼란, 복잡

응답 형식:
{{
  "제목": "대화 주제를 나타내는 간결한 제목 (최대 80자)",
  "대화 요약": "대화 내용 요약",
  "인사이트": "얻은 인사이트나 깨달음",
  "감정 상태": ["위 목록에서 해당하는 것 1~3개"],
  "태그": ["태그1", "태그2", "태그3"]
}}""",

    "review": """다음 텍스트에서 주간 리뷰 정보를 추출해서 JSON만 응답하세요. 다른 텍스트는 절대 넣지 마세요.

텍스트:
{text}

응답 형식:
{{
  "제목": "예: '2월 3주차 리뷰' (최대 80자)",
  "회사 성과": "이번 주 회사 업무 성과",
  "개인 성과": "이번 주 개인적 성과",
  "잘한 것": "이번 주 잘한 점",
  "개선할 것": "개선이 필요한 점",
  "다음 주 핵심 목표": "다음 주 목표",
  "컨디션 종합": "좋음/보통/힘들었음 중 정확히 하나"
}}""",
}


# ─── Claude 추출 ──────────────────────────────────────────
async def extract_data(text: str, command: str) -> dict:
    """Claude API로 텍스트에서 구조화된 데이터 추출"""
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    prompt = EXTRACT_PROMPTS[command].format(text=text)

    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    result_text = msg.content[0].text.strip()
    match = re.search(r"\{.*\}", result_text, re.DOTALL)
    if not match:
        raise ValueError("Claude 응답에서 JSON을 파싱할 수 없습니다.")

    data = json.loads(match.group())

    # Select 필드 유효성 검증
    if command == "health":
        data["컨디션"] = validate_select(data.get("컨디션", ""), "컨디션")
        data["수면"] = validate_select(data.get("수면", ""), "수면")
        data["영양제"] = validate_select(data.get("영양제", ""), "영양제", "안먹음")
    elif command == "growth":
        emotions = data.get("감정 상태", [])
        if isinstance(emotions, str):
            emotions = [emotions]
        data["감정 상태"] = validate_multi_select(emotions, "감정 상태")
    elif command == "review":
        data["컨디션 종합"] = validate_select(data.get("컨디션 종합", ""), "컨디션 종합")

    return data


# ─── 미리보기 포맷 ──────────────────────────────────────────
def format_preview(command: str, data: dict) -> str:
    if command == "health":
        return (
            f"🏥 *건강 기록 미리보기*\n\n"
            f"📅 *날짜*: {data.get('날짜', today_title())}\n"
            f"🍳 *아침*: {_trunc(data.get('아침', ''))}\n"
            f"🍱 *점심*: {_trunc(data.get('점심', ''))}\n"
            f"🍽️ *저녁*: {_trunc(data.get('저녁', ''))}\n"
            f"🍪 *간식*: {_trunc(data.get('간식', ''))}\n"
            f"💪 *운동*: {_trunc(data.get('운동', ''))}\n"
            f"😊 *컨디션*: {data.get('컨디션', '보통')}\n"
            f"😴 *수면*: {data.get('수면', '보통')}\n"
            f"💊 *영양제*: {data.get('영양제', '안먹음')}\n"
            f"⭐ *오늘 잘한 것*: {_trunc(data.get('오늘 잘한 것', ''))}\n"
            f"📝 *메모*: {_trunc(data.get('메모', ''))}"
        )

    elif command == "discuss":
        tags = " ".join(f"#{t}" for t in data.get("태그", []))
        url_line = f"\n🔗 *원본*: {data['원본URL']}" if data.get("원본URL") else ""
        return (
            f"💬 *토론 기록 미리보기*\n\n"
            f"📌 *제목*: {data.get('제목', '-')}"
            f"{url_line}\n"
            f"💡 *핵심 인사이트*: {_trunc(data.get('핵심 인사이트', ''))}\n"
            f"🎯 *내 삶 적용 포인트*: {_trunc(data.get('내 삶 적용 포인트', ''))}\n"
            f"🏷️ *태그*: {tags or '-'}"
        )

    elif command == "read":
        return (
            f"📚 *독서 기록 미리보기*\n\n"
            f"📌 *제목*: {data.get('제목', '-')}\n"
            f"📖 *책 이름*: {data.get('책 이름', '-')}\n"
            f"📄 *챕터/페이지*: {_trunc(data.get('챕터/페이지', ''))}\n"
            f"📝 *핵심 요약*: {_trunc(data.get('핵심 요약', ''))}\n"
            f"🎯 *내 삶 적용 포인트*: {_trunc(data.get('내 삶 적용 포인트', ''))}\n"
            f"✨ *인상 깊은 문장*: {_trunc(data.get('인상 깊은 문장', ''))}"
        )

    elif command == "growth":
        emotions = ", ".join(data.get("감정 상태", []))
        tags = " ".join(f"#{t}" for t in data.get("태그", []))
        return (
            f"🌱 *성장 기록 미리보기*\n\n"
            f"📌 *제목*: {data.get('제목', '-')}\n"
            f"📝 *대화 요약*: {_trunc(data.get('대화 요약', ''))}\n"
            f"💡 *인사이트*: {_trunc(data.get('인사이트', ''))}\n"
            f"💭 *감정 상태*: {emotions or '-'}\n"
            f"🏷️ *태그*: {tags or '-'}"
        )

    elif command == "review":
        return (
            f"📊 *주간 리뷰 미리보기*\n\n"
            f"📌 *제목*: {data.get('제목', '-')}\n"
            f"🏢 *회사 성과*: {_trunc(data.get('회사 성과', ''))}\n"
            f"🙋 *개인 성과*: {_trunc(data.get('개인 성과', ''))}\n"
            f"⭐ *잘한 것*: {_trunc(data.get('잘한 것', ''))}\n"
            f"🔧 *개선할 것*: {_trunc(data.get('개선할 것', ''))}\n"
            f"🎯 *다음 주 핵심 목표*: {_trunc(data.get('다음 주 핵심 목표', ''))}\n"
            f"😊 *컨디션 종합*: {data.get('컨디션 종합', '보통')}"
        )

    return "미리보기를 생성할 수 없습니다."


# ─── Notion 저장 ──────────────────────────────────────────
def _rt(text: str) -> dict:
    """rich_text 프로퍼티 생성 헬퍼"""
    return {"rich_text": [{"text": {"content": (text or "-")[:2000]}}]}


async def save_to_notion(command: str, data: dict) -> str:
    """추출된 데이터를 해당 Notion DB에 저장하고 페이지 URL 반환"""
    notion = NotionClient(auth=NOTION_API_KEY)
    db_id = NOTION_DB_IDS[command]
    today = today_iso()

    if command == "health":
        props = {
            "날짜": {"title": [{"text": {"content": data.get("날짜", today_title())}}]},
            "아침": _rt(data.get("아침")),
            "점심": _rt(data.get("점심")),
            "저녁": _rt(data.get("저녁")),
            "간식": _rt(data.get("간식")),
            "운동": _rt(data.get("운동")),
            "컨디션": {"select": {"name": data.get("컨디션", "보통")}},
            "수면": {"select": {"name": data.get("수면", "보통")}},
            "영양제": {"select": {"name": data.get("영양제", "안먹음")}},
            "오늘 잘한 것": _rt(data.get("오늘 잘한 것")),
            "메모": _rt(data.get("메모")),
        }

    elif command == "discuss":
        props = {
            "제목": {"title": [{"text": {"content": (data.get("제목") or "토론 기록")[:100]}}]},
            "핵심 인사이트": _rt(data.get("핵심 인사이트")),
            "내 삶 적용 포인트": _rt(data.get("내 삶 적용 포인트")),
            "날짜": {"date": {"start": today}},
            "태그": {"multi_select": [{"name": t[:100]} for t in data.get("태그", [])[:10]]},
        }
        if data.get("원본URL"):
            props["원본 콘텐츠"] = {"url": data["원본URL"]}

    elif command == "read":
        props = {
            "제목": {"title": [{"text": {"content": (data.get("제목") or "독서 기록")[:100]}}]},
            "책 이름": {"select": {"name": (data.get("책 이름") or "미정")[:100]}},
            "챕터/페이지": _rt(data.get("챕터/페이지")),
            "핵심 요약": _rt(data.get("핵심 요약")),
            "내 삶 적용 포인트": _rt(data.get("내 삶 적용 포인트")),
            "인상 깊은 문장": _rt(data.get("인상 깊은 문장")),
            "날짜": {"date": {"start": today}},
        }

    elif command == "growth":
        emotions = data.get("감정 상태", [])
        if isinstance(emotions, str):
            emotions = [emotions]
        props = {
            "제목": {"title": [{"text": {"content": (data.get("제목") or "성장 기록")[:100]}}]},
            "대화 요약": _rt(data.get("대화 요약")),
            "인사이트": _rt(data.get("인사이트")),
            "감정 상태": {"multi_select": [{"name": e[:100]} for e in emotions[:5]]},
            "날짜": {"date": {"start": today}},
            "태그": {"multi_select": [{"name": t[:100]} for t in data.get("태그", [])[:10]]},
        }

    elif command == "review":
        props = {
            "제목": {"title": [{"text": {"content": (data.get("제목") or "주간 리뷰")[:100]}}]},
            "회사 성과": _rt(data.get("회사 성과")),
            "개인 성과": _rt(data.get("개인 성과")),
            "잘한 것": _rt(data.get("잘한 것")),
            "개선할 것": _rt(data.get("개선할 것")),
            "다음 주 핵심 목표": _rt(data.get("다음 주 핵심 목표")),
            "컨디션 종합": {"select": {"name": data.get("컨디션 종합", "보통")}},
            "날짜": {"date": {"start": today}},
        }
    else:
        raise ValueError(f"알 수 없는 명령어: {command}")

    page = await notion.pages.create(
        parent={"database_id": db_id},
        properties=props,
    )
    page_id = page["id"].replace("-", "")
    return f"https://notion.so/{page_id}"


# ─── 주간 자동 리뷰 ──────────────────────────────────────────
def _week_range_kst() -> tuple[str, str]:
    """이번 주 월요일~일요일 날짜 범위 (KST 기준 ISO 문자열)"""
    now = datetime.now(KST)
    monday = now - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def _week_label() -> str:
    """'2월 3주차' 형태의 주차 라벨 생성"""
    now = datetime.now(KST)
    week_num = (now.day - 1) // 7 + 1
    return f"{now.month}월 {week_num}주차"


async def fetch_notion_week_data(db_id: str, date_prop: str) -> list[dict]:
    """Notion DB에서 이번 주 월~일 데이터를 조회"""
    if not db_id:
        return []
    notion = NotionClient(auth=NOTION_API_KEY)
    start, end = _week_range_kst()
    try:
        result = await notion.databases.query(
            database_id=db_id,
            filter={
                "property": date_prop,
                "date": {"on_or_after": start},
            },
        )
        # end 범위 필터 (on_or_before 추가)
        pages = []
        for page in result.get("results", []):
            prop = page.get("properties", {}).get(date_prop, {})
            date_val = None
            if prop.get("date"):
                date_val = prop["date"].get("start", "")
            elif prop.get("title"):
                # health DB는 title 프로퍼티가 날짜 역할
                date_val = None  # title 기반은 날짜 필터로 이미 처리됨
            if date_val and date_val > end:
                continue
            pages.append(page)
        return pages if pages else result.get("results", [])
    except Exception as e:
        logger.error(f"Notion 주간 데이터 조회 오류 ({db_id}): {e}")
        return []


def _extract_text_from_prop(prop: dict) -> str:
    """Notion 프로퍼티에서 텍스트 추출"""
    if prop.get("title"):
        return "".join(t.get("plain_text", "") for t in prop["title"])
    if prop.get("rich_text"):
        return "".join(t.get("plain_text", "") for t in prop["rich_text"])
    if prop.get("select"):
        return prop["select"].get("name", "")
    if prop.get("multi_select"):
        return ", ".join(s.get("name", "") for s in prop["multi_select"])
    if prop.get("date"):
        return prop["date"].get("start", "")
    if prop.get("url"):
        return prop["url"] or ""
    return ""


def summarize_pages(pages: list[dict], label: str) -> str:
    """페이지 목록을 텍스트 요약으로 변환"""
    if not pages:
        return f"[{label}] 이번 주 기록 없음"
    lines = [f"[{label}] 총 {len(pages)}건"]
    for i, page in enumerate(pages, 1):
        props = page.get("properties", {})
        parts = []
        for key, val in props.items():
            text = _extract_text_from_prop(val)
            if text and text != "-":
                parts.append(f"{key}: {text}")
        if parts:
            lines.append(f"  {i}. " + " / ".join(parts))
    return "\n".join(lines)


WEEKLY_REVIEW_PROMPT = """아래는 한 사람의 이번 주 (월~일) 기록 데이터입니다.
이 데이터를 바탕으로 주간 리뷰를 작성해주세요.

**중요 원칙**: 자책이나 부정적 판단 없이, 성장 관점으로 따뜻하게 작성해주세요.

데이터:
{data}

아래 JSON 형식으로만 응답하세요. 다른 텍스트는 절대 넣지 마세요.
{{
  "이번 주 활동 요약": "각 카테고리별 활동을 2~3문장으로 요약",
  "잘한 것": "데이터에서 발견된 긍정적 패턴이나 성과 (2~3가지)",
  "아쉬운 것": "판단 없이 패턴만 언급, 자책하지 않게 (1~2가지)",
  "다음 주 제안": "구체적이고 실현 가능한 제안 1가지",
  "컨디션 종합": "좋음/보통/힘들었음 중 정확히 하나 (건강 데이터 기반)"
}}"""


async def generate_weekly_review(app) -> None:
    """매주 일요일 19:00 KST에 실행되는 자동 주간 리뷰"""
    logger.info("주간 자동 리뷰 시작")

    if not chat_ids:
        logger.warning("저장된 chat_id가 없어 주간 리뷰를 전송할 수 없습니다.")
        return

    # 1) 4개 DB에서 이번 주 데이터 수집
    db_configs = [
        (NOTION_DB_IDS["health"], "날짜", "건강"),
        (NOTION_DB_IDS["discuss"], "날짜", "토론"),
        (NOTION_DB_IDS["read"], "날짜", "독서"),
        (NOTION_DB_IDS["growth"], "날짜", "성장"),
    ]

    all_summaries = []
    for db_id, date_prop, label in db_configs:
        pages = await fetch_notion_week_data(db_id, date_prop)
        summary = summarize_pages(pages, label)
        all_summaries.append(summary)

    combined_data = "\n\n".join(all_summaries)

    # 2) Claude AI로 주간 리뷰 생성
    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        prompt = WEEKLY_REVIEW_PROMPT.format(data=combined_data)

        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        result_text = msg.content[0].text.strip()
        match = re.search(r"\{.*\}", result_text, re.DOTALL)
        if not match:
            raise ValueError("Claude 응답에서 JSON을 파싱할 수 없습니다.")

        review = json.loads(match.group())
    except Exception as e:
        logger.error(f"주간 리뷰 AI 분석 오류: {e}")
        for cid in chat_ids:
            try:
                await app.bot.send_message(
                    chat_id=cid,
                    text=f"❌ 주간 자동 리뷰 생성 중 오류가 발생했습니다.\n\n`{e}`",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        return

    review["컨디션 종합"] = validate_select(review.get("컨디션 종합", ""), "컨디션 종합")
    week_label = _week_label()
    title = f"{week_label} 자동 리뷰"

    # 3) Notion에 저장
    page_url = ""
    try:
        notion = NotionClient(auth=NOTION_API_KEY)
        db_id = NOTION_DB_IDS.get("review")
        if db_id:
            today = today_iso()
            props = {
                "제목": {"title": [{"text": {"content": title}}]},
                "회사 성과": _rt(review.get("이번 주 활동 요약")),
                "개인 성과": _rt(review.get("이번 주 활동 요약")),
                "잘한 것": _rt(review.get("잘한 것")),
                "개선할 것": _rt(review.get("아쉬운 것")),
                "다음 주 핵심 목표": _rt(review.get("다음 주 제안")),
                "컨디션 종합": {"select": {"name": review.get("컨디션 종합", "보통")}},
                "날짜": {"date": {"start": today}},
            }
            page = await notion.pages.create(
                parent={"database_id": db_id},
                properties=props,
            )
            page_id = page["id"].replace("-", "")
            page_url = f"https://notion.so/{page_id}"
    except Exception as e:
        logger.error(f"주간 리뷰 Notion 저장 오류: {e}")

    # 4) 텔레그램으로 리뷰 전송
    review_msg = (
        f"📊 *{title}*\n\n"
        f"📋 *이번 주 활동 요약*\n{review.get('이번 주 활동 요약', '-')}\n\n"
        f"⭐ *잘한 것*\n{review.get('잘한 것', '-')}\n\n"
        f"💭 *아쉬운 것*\n{review.get('아쉬운 것', '-')}\n\n"
        f"🎯 *다음 주 제안*\n{review.get('다음 주 제안', '-')}\n\n"
        f"😊 *컨디션 종합*: {review.get('컨디션 종합', '보통')}\n"
    )
    if page_url:
        review_msg += f"\n🔗 [Notion에서 보기]({page_url})\n"

    review_msg += "\n─────────────────\n💬 *이번 주 한 줄 소감이 있다면 답장해주세요!*"

    for cid in chat_ids:
        try:
            await app.bot.send_message(
                chat_id=cid,
                text=review_msg,
                parse_mode="Markdown",
            )
            # 한 줄 소감 대기 상태 등록
            if page_url:
                # page_id를 저장해서 나중에 업데이트에 사용
                notion_page_id = page["id"]
                waiting_for_comment[cid] = notion_page_id
        except Exception as e:
            logger.error(f"주간 리뷰 전송 오류 (chat_id={cid}): {e}")

    logger.info("주간 자동 리뷰 완료")


async def handle_weekly_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """한 줄 소감 답장 처리. 처리했으면 True, 아니면 False 반환"""
    cid = update.effective_chat.id
    if cid not in waiting_for_comment:
        return False

    comment = (update.message.text or "").strip()
    if not comment:
        return False

    notion_page_id = waiting_for_comment.pop(cid)

    try:
        notion = NotionClient(auth=NOTION_API_KEY)
        # 기존 "메모" 또는 별도 필드에 한 줄 소감 추가
        await notion.pages.update(
            page_id=notion_page_id,
            properties={
                "개인 성과": _rt(f"[한 줄 소감] {comment}"),
            },
        )
        await update.message.reply_text(
            f"✅ 한 줄 소감이 Notion 리뷰에 저장되었습니다!\n\n💬 \"{comment}\"",
        )
    except Exception as e:
        logger.error(f"한 줄 소감 저장 오류: {e}")
        await update.message.reply_text(
            f"❌ 소감 저장 중 오류가 발생했습니다.\n\n`{e}`",
            parse_mode="Markdown",
        )

    return True


async def handle_plain_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """일반 메시지 핸들러: chat_id 저장 + 한 줄 소감 처리"""
    cid = update.effective_chat.id
    chat_ids.add(cid)

    # 한 줄 소감 대기 중이면 처리
    handled = await handle_weekly_comment(update, context)
    if handled:
        return

    # 명령어가 아닌 일반 메시지는 안내
    await update.message.reply_text(
        "💡 명령어와 함께 텍스트를 입력해주세요.\n\n"
        "/help 로 사용법을 확인할 수 있습니다."
    )


# ─── 인라인 키보드 ──────────────────────────────────────────
def preview_kb(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 저장", callback_data=f"save:{sid}"),
            InlineKeyboardButton("❌ 취소", callback_data=f"cancel:{sid}"),
        ],
    ])


def retry_kb(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 재시도", callback_data=f"save:{sid}"),
        InlineKeyboardButton("❌ 취소", callback_data=f"cancel:{sid}"),
    ]])


# ─── 핸들러 ──────────────────────────────────────────
COMMAND_LABELS = {
    "health": "🏥 건강 기록",
    "discuss": "💬 토론 기록",
    "read": "📚 독서 기록",
    "growth": "🌱 성장 기록",
    "review": "📊 주간 리뷰",
}


async def handle_record(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str):
    """모든 기록 명령어의 공통 처리 로직"""
    chat_ids.add(update.effective_chat.id)
    text = update.message.text or ""

    # 명령어 접두사 제거
    prefix = f"/{command}"
    if text.startswith(prefix):
        text = text[len(prefix):].strip()

    if not text:
        await update.message.reply_text(
            f"⚠️ 기록할 텍스트를 함께 입력해주세요.\n\n"
            f"사용법: /{command} [Claude 대화 요약 복붙]"
        )
        return

    if not NOTION_DB_IDS.get(command):
        await update.message.reply_text(f"⚠️ {command} Notion DB ID가 설정되지 않았습니다.")
        return

    msg = await update.message.reply_text("🔍 AI 분석 중... 잠시만 기다려주세요.")

    try:
        data = await extract_data(text, command)

        if command == "health":
            data["날짜"] = today_title()

        sid = uuid.uuid4().hex[:8]
        sessions[sid] = {"command": command, "data": data}

        preview = format_preview(command, data)
        full_msg = f"{preview}\n\n{'─' * 20}\n저장하시겠습니까?"

        if len(full_msg) > 4096:
            full_msg = full_msg[:4090] + "\n..."

        await msg.edit_text(
            full_msg,
            parse_mode="Markdown",
            reply_markup=preview_kb(sid),
        )
    except Exception as e:
        logger.error(f"/{command} 처리 오류: {e}")
        await msg.edit_text(
            f"❌ 분석 중 오류가 발생했습니다.\n\n`{e}`\n\n명령어와 텍스트를 다시 전송해주세요.",
            parse_mode="Markdown",
        )


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_record(update, ctx, "health")


async def cmd_discussion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_record(update, ctx, "discuss")


async def cmd_reading(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_record(update, ctx, "read")


async def cmd_growth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_record(update, ctx, "growth")


async def cmd_review(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_record(update, ctx, "review")


async def cmd_weekly_review_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """수동 주간 리뷰 트리거 (테스트용)"""
    chat_ids.add(update.effective_chat.id)
    await update.message.reply_text("🔄 주간 자동 리뷰를 수동 실행합니다...")
    await generate_weekly_review(context.application)


async def handle_callback(update: Update, _: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    parts = data.split(":", 1)
    action = parts[0]
    sid = parts[1] if len(parts) > 1 else ""

    if action == "save":
        if sid not in sessions:
            await query.edit_message_text("❌ 세션이 만료되었습니다. 다시 시도해주세요.")
            return

        await query.edit_message_text("💾 Notion에 저장 중...")
        session = sessions[sid]

        try:
            page_url = await save_to_notion(session["command"], session["data"])
            label = COMMAND_LABELS.get(session["command"], "기록")
            sessions.pop(sid, None)

            await query.edit_message_text(
                f"✅ *저장 완료!*\n\n"
                f"{label}\n\n"
                f"🔗 [Notion에서 보기]({page_url})",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Notion 저장 오류: {e}")
            await query.edit_message_text(
                f"❌ *저장 실패*\n\n`{e}`\n\n재시도하시겠습니까?",
                parse_mode="Markdown",
                reply_markup=retry_kb(sid),
            )

    elif action == "cancel":
        sessions.pop(sid, None)
        await query.edit_message_text("❌ 취소되었습니다.")


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat_ids.add(update.effective_chat.id)
    await update.message.reply_text(
        "👋 *기록봇에 오신 것을 환영합니다!*\n\n"
        "Claude 프로젝트 대화 요약을\n"
        "Notion에 자동 저장하는 봇입니다.\n\n"
        "📋 *사용 가능한 명령어*\n\n"
        "🏥 /health — 건강 기록 (식사/운동/컨디션)\n"
        "💬 /discuss — 콘텐츠 토론 기록\n"
        "📚 /read — 독서 기록\n"
        "🌱 /growth — 내면 성장 기록\n"
        "📊 /review — 주간 리뷰\n\n"
        "📖 /help — 자세한 사용법\n\n"
        "💡 사용법: /명령어 [Claude 대화 요약 복붙]",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat_ids.add(update.effective_chat.id)
    await update.message.reply_text(
        "📖 *기록봇 상세 사용법*\n\n"
        "Claude 프로젝트에서 대화 후 요약을 복사해서\n"
        "아래 명령어와 함께 전송하면\n"
        "AI가 자동 추출 후 Notion에 저장합니다.\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "🏥 */health* [텍스트]\n"
        "식사, 운동, 컨디션, 수면 등 자동 추출\n"
        "→ Claude ⑦번 프로젝트 요약 복붙\n\n"
        "💬 */discuss* [텍스트]\n"
        "제목, 인사이트, 적용 포인트, 태그 추출\n"
        "→ Claude ④번 프로젝트 요약 복붙\n\n"
        "📚 */read* [텍스트]\n"
        "책 이름, 핵심 요약, 인상 깊은 문장 추출\n"
        "→ Claude ⑤번 프로젝트 요약 복붙\n\n"
        "🌱 */growth* [텍스트]\n"
        "대화 요약, 인사이트, 감정 상태 추출\n"
        "→ Claude ⑥번 프로젝트 요약 복붙\n\n"
        "📊 */review* [텍스트]\n"
        "성과, 잘한 것, 개선점, 다음 주 목표 추출\n"
        "→ Claude ⑨번 프로젝트 요약 복붙\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "💡 *사용 흐름*\n"
        "1. Claude 프로젝트에서 대화\n"
        "2. Claude가 요약 정리\n"
        "3. 요약 복사\n"
        "4. 기록봇에 /명령어 + 붙여넣기\n"
        "5. 미리보기 확인 → 저장 클릭\n"
        "6. Notion에 자동 저장!",
        parse_mode="Markdown",
    )


# ─── 메인 ──────────────────────────────────────────
def main():
    missing = [k for k, v in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "NOTION_API_KEY": NOTION_API_KEY,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    }.items() if not v]
    if missing:
        raise EnvironmentError(f"필수 환경변수 누락: {', '.join(missing)}\n.env 파일을 확인하세요.")

    missing_dbs = [cmd for cmd, db_id in NOTION_DB_IDS.items() if not db_id]
    if missing_dbs:
        logger.warning(f"Notion DB ID 누락 (해당 명령어 사용 불가): {', '.join(missing_dbs)}")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("discuss", cmd_discussion))
    app.add_handler(CommandHandler("read", cmd_reading))
    app.add_handler(CommandHandler("growth", cmd_growth))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("weekly", cmd_weekly_review_now))
    app.add_handler(CallbackQueryHandler(handle_callback))
    # 일반 텍스트 메시지 핸들러 (chat_id 저장 + 한 줄 소감 처리)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_message))

    # APScheduler: 매주 일요일 19:00 KST 자동 주간 리뷰
    scheduler = AsyncIOScheduler(timezone=KST)
    scheduler.add_job(
        generate_weekly_review,
        CronTrigger(day_of_week="sun", hour=19, minute=0, timezone=KST),
        args=[app],
        id="weekly_review",
        name="주간 자동 리뷰",
    )
    scheduler.start()
    logger.info("APScheduler 시작: 매주 일요일 19:00 KST 주간 자동 리뷰")

    logger.info("기록봇 시작!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
