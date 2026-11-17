"""후기·닉네임 검사기 (금지어, 전화번호·링크·주소)."""
import re

# 기본 금지어: 욕설, 비하 표현, 조리해 주시는 분들을 낮춰 부르는 말.
# 선생님이 관리자 화면에서 추가한 금지어는 이 목록에 더해져요.
DEFAULT_BANNED_WORDS = [
    "씨발", "시발", "씨빨", "ㅅㅂ", "ㅆㅂ", "십새", "ㅅㄲ",
    "병신", "븅신", "ㅂㅅ", "개새", "새끼", "존나", "졸라", "ㅈㄴ", "좆",
    "지랄", "ㅈㄹ", "염병", "닥쳐", "꺼져", "미친", "ㅁㅊ", "또라이", "돌아이",
    "멍청", "바보", "찐따", "장애인", "애미", "느금", "엠창",
    "뒤져", "뒈져", "죽여", "쓰레기", "개같", "역겨", "토나와", "토할",
    "아줌마", "급식충", "틀딱", "fuck", "shit", "bitch",
]

_PHONE_RE = re.compile(r"01[016789][\s.-]?\d{3,4}[\s.-]?\d{4}|\d{2,3}[\s.-]\d{3,4}[\s.-]\d{4}")
_LINK_RE = re.compile(r"https?:|www\.|\.(com|net|kr|org|io|me|ly)\b|@[a-z0-9]", re.IGNORECASE)
_ADDRESS_RE = re.compile(r"\d+\s*동\s*\d+\s*호")
_NOT_LETTER_RE = re.compile(r"[^가-힣ㄱ-ㅎㅏ-ㅣa-z]")

MESSAGES = {
    "badword": "고운 말로 다시 써 볼까요? 음식 맛, 양, 온도에 대해 써 주세요.",
    "personal": "전화번호, 주소, 링크처럼 보이는 글은 쓸 수 없어요.",
}


def normalize(text: str) -> str:
    """띄어쓰기·숫자·기호를 빼서 '씨 1 발' 같은 우회를 막아요.
    그래서 '호박죽 어때'가 '죽어'에 걸리는 것처럼 두 낱말이 붙어서 걸릴 수 있으니,
    금지어는 충분히 구체적인 말로 넣어 주세요."""
    return _NOT_LETTER_RE.sub("", (text or "").lower())


def check_text(text: str, extra_words: list[str] | None = None) -> str | None:
    """문제가 없으면 None, 있으면 'badword' 또는 'personal'."""
    raw = text or ""
    if _PHONE_RE.search(raw) or _LINK_RE.search(raw) or _ADDRESS_RE.search(raw):
        return "personal"
    flat = normalize(raw)
    for word in DEFAULT_BANNED_WORDS + list(extra_words or []):
        w = normalize(word)
        if w and w in flat:
            return "badword"
    return None
