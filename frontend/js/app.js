// 학생 화면: 로그인·회원가입 → 오늘의 급식 → 메뉴 상세
import { api, getToken, setToken } from "./api.js";
import { todayKst, addDays, prettyDate, isWeekend, el, starText, toast } from "./util.js";

const COMMENT_MAX = 50;
const POLL_MS = 5000; // 5초마다 새로 불러와서 친구들 평가가 보이게 해요
const RULES = [
  "음식에 대해서만 써요.",
  "친구나 다른 사람의 이름을 쓰지 않아요.",
  "고운 말을 써요.",
  "이상한 글을 보면 신고 버튼을 눌러요.",
];

const appEl = document.getElementById("app");
const whoEl = document.getElementById("who");
const dialogEl = document.getElementById("detail");

const state = {
  nickname: null,
  date: todayKst(),
  today: todayKst(), // 서버가 알려주는 오늘 날짜로 바뀌어요
  menus: null, // null = 아직 불러오는 중
  events: [], // 나이스 학사일정 (그날 행사, 방학·재량휴업일)
  detailMenuId: null,
};
let pollTimer = null;

// ---------- 시작 ----------

start();

async function start() {
  if (!getToken("student")) return showHome();
  try {
    const me = await api("student", "/api/me");
    state.nickname = me.nickname;
    if (me.needs_account) return showMakeAccount();
    showMain();
  } catch (err) {
    if (err.status === 401) setToken("student", null);
    showHome("login", err.status === 401 ? "" : err.message);
  }
}

async function logout() {
  if (!confirm("로그아웃할까요?")) return;
  await api("student", "/api/logout", { method: "POST" }).catch(() => {});
  setToken("student", null);
  state.nickname = null;
  if (dialogEl.open) dialogEl.close();
  showHome("login");
}

function showWho() {
  whoEl.hidden = false;
  whoEl.replaceChildren(`🙂 ${state.nickname} `, el("button", { class: "btn link", type: "button", onclick: logout }, "로그아웃"));
}

// ---------- 1. 홈: 로그인 · 회원가입 ----------

function field(id, label, props = {}) {
  const input = el("input", { id, name: id, autocomplete: "off", required: true, ...props });
  return { input, nodes: [el("label", { for: id }, label), input] };
}

function rulesBox() {
  return el("div", { class: "rules" },
    el("h3", {}, "이용 규칙"),
    el("ol", {}, RULES.map((r) => el("li", {}, r))),
  );
}

// 버튼을 누르는 동안 "확인하는 중…" 으로 바꾸고, 틀리면 빨간 글로 알려줘요.
function submitter(btn, err, label, fn) {
  return async (e) => {
    e.preventDefault();
    err.textContent = "";
    btn.disabled = true;
    btn.textContent = "확인하는 중…";
    try {
      await fn();
    } catch (ex) {
      err.textContent = ex.message;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  };
}

function signedIn(res) {
  setToken("student", res.token);
  state.nickname = res.nickname;
  showMain();
}

function showHome(mode = "login", message = "") {
  whoEl.hidden = true;
  stopPolling();
  const tabs = el("nav", { class: "tabs", role: "tablist" },
    [["login", "로그인"], ["signup", "회원가입"]].map(([id, label]) =>
      el("button", { class: `tab${id === mode ? " on" : ""}`, role: "tab", type: "button", "aria-selected": String(id === mode), onclick: () => showHome(id) }, label)));
  appEl.replaceChildren(tabs, mode === "login" ? loginForm(message) : signupForm(message));
}

function loginForm(message) {
  const id = field("login-id", "아이디", { maxlength: 12, autocomplete: "username", autocapitalize: "off" });
  const pw = field("login-pw", "비밀번호", { type: "password", maxlength: 30, autocomplete: "current-password" });
  const err = el("p", { class: "error", role: "alert" }, message);
  const btn = el("button", { class: "btn primary big", type: "submit" }, "로그인");
  return el("form", { class: "panel join", onsubmit: submitter(btn, err, "로그인", async () => {
    signedIn(await api(null, "/api/login", { method: "POST", body: { login_id: id.input.value, password: pw.input.value } }));
  }) },
    el("h2", {}, "우리 반 급식 평가판"),
    ...id.nodes, ...pw.nodes,
    err, btn,
    el("p", { class: "hint" }, "처음이면 위의 회원가입을 눌러요. 비밀번호를 잊어버렸으면 선생님께 말해 주세요."),
  );
}

// 회원가입과 '계정 만들기'에 같이 쓰는 칸: 학년-반, 진짜 이름, 아이디, 비밀번호
function accountFields() {
  const cls = field("class", "학년-반", { maxlength: 5, placeholder: "예: 5-1", pattern: "\\s*[1-6]\\s*-\\s*\\d{1,2}\\s*", title: "5-1 처럼 학년과 반을 숫자로 써 주세요" });
  const real = field("real-name", "진짜 이름", { maxlength: 10, placeholder: "예: 김하안" });
  const id = field("new-id", "아이디", { maxlength: 12, autocomplete: "username", autocapitalize: "off", placeholder: "영어 소문자와 숫자 4~12글자", pattern: "[A-Za-z0-9]{4,12}", title: "영어와 숫자로 4~12글자" });
  const pw = field("new-pw", "비밀번호", { type: "password", minlength: 4, maxlength: 30, autocomplete: "new-password", placeholder: "4글자 이상" });
  const pw2 = field("new-pw2", "비밀번호 한 번 더", { type: "password", minlength: 4, maxlength: 30, autocomplete: "new-password" });
  const privacy = el("p", { class: "notice" }, "🔒 진짜 이름은 선생님만 볼 수 있어요. 친구들에게는 닉네임만 보여요.");
  return {
    cls, real, id, pw,
    top: [...cls.nodes, ...real.nodes, privacy],
    bottom: [...id.nodes, ...pw.nodes, ...pw2.nodes],
    check() {
      if (pw.input.value !== pw2.input.value) throw new Error("비밀번호 두 개가 서로 달라요.");
    },
  };
}

function signupForm(message) {
  const f = accountFields();
  const nick = field("nick", "닉네임", { maxlength: 10, placeholder: "예: 떡볶이왕" });
  const err = el("p", { class: "error", role: "alert" }, message);
  const btn = el("button", { class: "btn primary big", type: "submit" }, "가입하기");
  return el("form", { class: "panel join", onsubmit: submitter(btn, err, "가입하기", async () => {
    f.check();
    signedIn(await api(null, "/api/signup", { method: "POST", body: {
      class_name: f.cls.input.value, real_name: f.real.input.value, nickname: nick.input.value,
      login_id: f.id.input.value, password: f.pw.input.value,
    } }));
  }) },
    el("h2", {}, "회원가입"),
    ...f.top,
    ...nick.nodes,
    el("p", { class: "hint" }, "친구들에게 보이는 이름이에요. 진짜 이름 말고 별명을 써 주세요."),
    ...f.bottom,
    rulesBox(),
    err, btn,
  );
}

// 예전에 입장 코드로 들어온 친구: 지금 닉네임과 후기는 그대로 두고 아이디·비밀번호만 만들어요.
function showMakeAccount(message = "") {
  stopPolling();
  showWho();
  const f = accountFields();
  const err = el("p", { class: "error", role: "alert" }, message);
  const btn = el("button", { class: "btn primary big", type: "submit" }, "계정 만들기");
  appEl.replaceChildren(el("form", { class: "panel join", onsubmit: submitter(btn, err, "계정 만들기", async () => {
    f.check();
    await api("student", "/api/me/account", { method: "POST", body: {
      class_name: f.cls.input.value, real_name: f.real.input.value, login_id: f.id.input.value, password: f.pw.input.value,
    } });
    toast("계정을 만들었어요! 다음부터는 아이디로 로그인해요.");
    showMain();
  }) },
    el("h2", {}, "아이디와 비밀번호를 만들어 주세요"),
    el("p", { class: "hint" }, `이제 입장 코드 대신 로그인으로 들어와요. 닉네임 '${state.nickname}'과 지금까지 남긴 별점·후기는 그대로예요.`),
    ...f.top, ...f.bottom,
    err, btn,
  ));
}

// ---------- 2. 오늘의 급식 ----------

function showMain() {
  showWho();
  goTo(state.date);
}

function goTo(date) {
  state.date = date;
  state.menus = null;
  state.events = [];
  renderMain();
  refresh();
  stopPolling();
  pollTimer = setInterval(() => {
    if (document.visibilityState === "visible") refresh();
  }, POLL_MS);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

async function refresh() {
  const date = state.date;
  try {
    const data = await api("student", `/api/days/${date}`);
    if (date !== state.date) return; // 그사이 날짜를 바꿨으면 버려요
    state.today = data.today;
    state.menus = data.menus;
    state.events = data.events || [];
    renderMain();
    if (dialogEl.open) renderDetailLive();
  } catch (err) {
    if (err.status === 401) {
      setToken("student", null);
      if (dialogEl.open) dialogEl.close();
      return showHome("login", err.message);
    }
    if (err.status === 403 && !state.menus) return showMakeAccount();
    toast(err.message);
  }
}

// "겨울방학" → "이라", "추석 연휴" → "라"
function iraParticle(word) {
  const code = word.charCodeAt(word.length - 1) - 0xac00;
  return code >= 0 && code < 11172 && code % 28 !== 0 ? "이라" : "라";
}

const isToday = () => state.date === state.today;
const findMenu = (id) => (state.menus || []).find((m) => m.id === id);

function renderMain() {
  const nav = el("nav", { class: "datenav", "aria-label": "날짜 바꾸기" },
    el("button", { class: "btn icon", "aria-label": "전날", onclick: () => goTo(addDays(state.date, -1)) }, "◀"),
    el("strong", {}, isToday() ? `오늘 · ${prettyDate(state.date)}` : prettyDate(state.date)),
    el("button", { class: "btn icon", "aria-label": "다음날", disabled: state.date >= state.today, onclick: () => goTo(addDays(state.date, 1)) }, "▶"),
  );

  let body;
  if (state.menus === null) {
    body = el("p", { class: "loading" }, "불러오는 중이에요…");
  } else if (state.menus.length === 0) {
    const off = state.events.find((e) => e.no_school);
    const msg = off
      ? `${off.name}${iraParticle(off.name)} 급식이 없어요`
      : isWeekend(state.date) ? "오늘은 급식이 없는 날이에요"
      : isToday() ? "오늘 메뉴가 아직 올라오지 않았어요" : "이날은 메뉴가 없어요";
    body = el("p", { class: "empty" }, msg);
  } else {
    body = el("div", { class: "cards" }, state.menus.map(menuCard));
  }

  const events = state.events.length
    ? el("p", { class: "event" }, "📅 ", state.events.map((e) => e.name).join(" · "))
    : null;
  appEl.replaceChildren(...[
    nav,
    events,
    isToday() ? null : el("p", { class: "notice" }, "지난 급식은 보기만 할 수 있어요. 평가는 그날에만 할 수 있어요."),
    body,
  ].filter(Boolean));
}

function menuCard(menu) {
  return el("article", { class: "card" },
    el("button", { class: "card-main", onclick: () => openDetail(menu.id), "aria-label": `${menu.name} 자세히 보기` },
      el("h3", {}, menu.name),
      el("p", { class: "avg" }, menu.avg === null ? "아직 평가가 없어요" : `★ ${menu.avg.toFixed(1)} · ${menu.count}명 평가`),
    ),
    isToday() ? starPicker(menu, menu.mine ? menu.mine.stars : 0, (n) => saveReview(menu, n, menu.mine ? menu.mine.comment : "")) : null,
  );
}

function starPicker(menu, current, onPick) {
  return el("div", { class: "stars", role: "group", "aria-label": `${menu.name} 별점` },
    [1, 2, 3, 4, 5].map((n) =>
      el("button", {
        class: `star${n <= current ? " on" : ""}`,
        type: "button",
        "aria-label": `별 ${n}개`,
        "aria-pressed": String(n === current),
        onclick: () => onPick(n),
      }, "★"),
    ),
  );
}

// ---------- 저장·지우기·신고 ----------

async function saveReview(menu, stars, comment) {
  comment = (comment || "").trim();
  if (comment.length > COMMENT_MAX) {
    toast(`후기는 ${COMMENT_MAX}자까지 쓸 수 있어요.`);
    return false;
  }
  try {
    await api("student", `/api/menus/${menu.id}/review`, { method: "PUT", body: { stars, comment } });
    toast("저장했어요!");
    await refresh();
    return true;
  } catch (err) {
    toast(err.message);
    return false;
  }
}

async function deleteMine(menu) {
  if (!confirm("내 별점과 후기를 지울까요?")) return;
  try {
    await api("student", `/api/menus/${menu.id}/review`, { method: "DELETE" });
    toast("지웠어요.");
    await refresh();
    renderDetailForm();
  } catch (err) {
    toast(err.message);
  }
}

async function toggleLike(review) {
  try {
    await api("student", `/api/reviews/${review.id}/like`, { method: review.liked ? "DELETE" : "PUT" });
    await refresh();
  } catch (err) {
    toast(err.message);
  }
}

async function report(review) {
  if (!confirm("이 후기를 선생님께 신고할까요?")) return;
  try {
    const res = await api("student", `/api/reviews/${review.id}/report`, { method: "POST" });
    toast(res.hidden ? "신고했어요. 이 후기는 숨겨졌어요." : "신고했어요. 선생님이 확인할 거예요.");
    await refresh();
  } catch (err) {
    toast(err.message);
  }
}

// ---------- 3. 메뉴 상세 ----------

let detailParts = null;

function openDetail(menuId) {
  const menu = findMenu(menuId);
  if (!menu) return;
  state.detailMenuId = menuId;
  detailParts = {
    head: el("div", { class: "detail-head" }),
    form: el("div", { class: "detail-form" }),
    list: el("div", { class: "detail-list" }),
  };
  dialogEl.replaceChildren(
    el("div", { class: "detail-top" },
      el("h2", { id: "detail-title" }, menu.name),
      el("button", { class: "btn icon", "aria-label": "닫기", onclick: () => dialogEl.close() }, "✕"),
    ),
    detailParts.head, detailParts.form, detailParts.list,
  );
  renderDetailForm();
  renderDetailLive();
  dialogEl.showModal();
}

dialogEl.addEventListener("close", () => {
  state.detailMenuId = null;
  detailParts = null;
});

// 입력 중인 글이 지워지지 않도록 입력칸은 열 때(그리고 저장·지운 뒤)만 다시 그려요.
function renderDetailForm() {
  const menu = findMenu(state.detailMenuId);
  if (!menu || !detailParts) return;
  if (!isToday()) {
    detailParts.form.replaceChildren();
    return;
  }
  const mine = menu.mine;
  let picked = mine ? mine.stars : 0;
  const counter = el("span", { class: "counter" });
  const textarea = el("textarea", {
    id: "comment", maxlength: COMMENT_MAX, rows: 2,
    placeholder: "음식 맛, 양, 온도에 대해 써 주세요 (안 써도 돼요)",
  });
  textarea.value = mine ? mine.comment : "";
  const updateCounter = () => (counter.textContent = `${textarea.value.length}/${COMMENT_MAX}`);
  textarea.addEventListener("input", updateCounter);
  updateCounter();

  const pickerWrap = el("div");
  const drawPicker = () => pickerWrap.replaceChildren(starPicker(menu, picked, (n) => { picked = n; drawPicker(); }));
  drawPicker();

  const saveBtn = el("button", { class: "btn primary", type: "button", onclick: async () => {
    if (!picked) return toast("별점을 먼저 골라 주세요.");
    saveBtn.disabled = true;
    const ok = await saveReview(menu, picked, textarea.value);
    saveBtn.disabled = false;
    if (ok) renderDetailForm();
  } }, mine ? "고쳐서 저장" : "저장");

  detailParts.form.replaceChildren(
    el("h3", {}, "내 평가"),
    pickerWrap,
    el("label", { for: "comment", class: "sr-only" }, "한 줄 후기"),
    textarea,
    el("div", { class: "form-row" },
      counter,
      mine ? el("button", { class: "btn ghost", type: "button", onclick: () => deleteMine(findMenu(menu.id)) }, "지우기") : null,
      saveBtn,
    ),
  );
}

function renderDetailLive() {
  const menu = findMenu(state.detailMenuId);
  if (!menu || !detailParts) return;
  detailParts.head.replaceChildren(...[
    el("p", { class: "avg big" }, menu.avg === null ? "아직 평가가 없어요" : `반 평균 ★ ${menu.avg.toFixed(1)} (${menu.count}명)`),
    menu.mine && menu.mine.hidden ? el("p", { class: "notice" }, "내 후기가 숨겨졌어요. 선생님이 확인하면 다시 보일 수 있어요.") : null,
  ].filter(Boolean));

  const list = menu.reviews; // 서버가 최신순으로 보내요
  detailParts.list.replaceChildren(
    el("h3", {}, `후기 ${list.length}개`),
    list.length === 0
      ? el("p", { class: "empty small" }, "아직 후기가 없어요. 첫 후기를 남겨 볼까요?")
      : el("ul", { class: "reviews" }, list.map((r) =>
          el("li", {},
            el("div", { class: "review-head" },
              el("strong", {}, r.nickname),
              el("span", { class: "review-stars", "aria-label": `별 ${r.stars}개` }, starText(r.stars)),
            ),
            el("p", {}, r.comment),
            el("div", { class: "review-actions" },
              r.mine
                ? el("span", { class: "like-count" }, `♥ ${r.likes}`)
                : el("button", {
                    class: `btn like${r.liked ? " on" : ""}`, type: "button", "aria-pressed": String(r.liked),
                    "aria-label": `좋아요 ${r.likes}개${r.liked ? ", 누름" : ""}`, onclick: () => toggleLike(r),
                  }, `${r.liked ? "♥" : "♡"} ${r.likes}`),
              r.mine
                ? el("span", { class: "tag" }, "내 후기")
                : el("button", { class: "btn link", type: "button", onclick: () => report(r) }, "신고"),
            ),
          ))),
  );
}
