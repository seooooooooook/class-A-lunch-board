// 선생님 화면: 메뉴 확인·수정(나이스는 서버가 매일 자동으로 불러와요) · 후기 관리 · 학생과 입장 코드
import { api, getToken, setToken } from "./api.js";
import { todayKst, prettyDate, average, el, starText, toast } from "./util.js";

const appEl = document.getElementById("app");
const whoEl = document.getElementById("who");
const TABS = [
  { id: "menus", label: "메뉴" },
  { id: "reviews", label: "후기 관리" },
  { id: "class", label: "학생 · 입장 코드" },
];
let currentTab = "menus";

const call = (path, opts) => api("teacher", path, opts);

// 어느 화면에서든 로그인이 풀리면 로그인 화면으로
async function safe(fn) {
  try {
    return await fn();
  } catch (err) {
    if (err.status === 401) {
      setToken("teacher", null);
      showLogin(err.message);
    } else {
      toast(err.message);
    }
    throw err;
  }
}

// ---------- 로그인 ----------

if (getToken("teacher")) showTabs();
else showLogin();

function showLogin(message = "") {
  whoEl.hidden = true;
  const pw = el("input", { id: "pw", type: "password", required: true, autocomplete: "current-password" });
  const err = el("p", { class: "error", role: "alert" }, message);
  appEl.replaceChildren(el("form", { class: "panel", onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    try {
      const res = await api(null, "/api/teacher/login", { method: "POST", body: { password: pw.value } });
      setToken("teacher", res.token);
      showTabs();
    } catch (ex) {
      err.textContent = ex.message;
    }
  } },
    el("h2", {}, "선생님 로그인"),
    el("label", { for: "pw" }, "비밀번호"), pw,
    el("p", { class: "hint" }, "backend/.env 의 TEACHER_PASSWORD 에 적은 비밀번호예요."),
    err,
    el("button", { class: "btn primary big", type: "submit" }, "로그인"),
  ));
}

async function logout() {
  await call("/api/teacher/logout", { method: "POST" }).catch(() => {});
  setToken("teacher", null);
  showLogin();
}

function showTabs() {
  whoEl.hidden = false;
  whoEl.replaceChildren("선생님 ", el("button", { class: "btn link", onclick: logout }, "로그아웃"));
  const nav = el("nav", { class: "tabs", role: "tablist" }, TABS.map((t) =>
    el("button", {
      class: `tab${t.id === currentTab ? " on" : ""}`, role: "tab", "aria-selected": String(t.id === currentTab),
      onclick: () => { currentTab = t.id; showTabs(); },
    }, t.label)));
  const body = el("section", { class: "panel" });
  appEl.replaceChildren(nav, body);
  ({ menus: renderMenus, reviews: renderReviews, class: renderClass })[currentTab](body);
}

// ---------- 1. 메뉴 (나이스 자동 불러오기 결과 + 직접 고치기) ----------

function renderMenus(box) {
  let rows = []; // { id?, name, source, original?, reviewCount }
  const dateInput = el("input", { type: "date", value: todayKst(), "aria-label": "날짜" });
  const listEl = el("ol", { class: "menu-edit" });
  const status = el("p", { class: "hint", role: "status" });
  const neisBox = el("div", { class: "notice" });

  async function load() {
    status.textContent = "불러오는 중…";
    const data = await safe(() => call(`/api/teacher/menus?date=${dateInput.value}`));
    rows = data.menus.map((m) => ({ id: m.id, name: m.name, source: m.source, original: m.name, reviewCount: m.review_count }));
    status.textContent = rows.length ? `${prettyDate(dateInput.value)}에 저장된 메뉴 ${rows.length}개` : `${prettyDate(dateInput.value)}에는 아직 메뉴가 없어요.`;
    draw();
  }

  function draw() {
    listEl.replaceChildren(...rows.map((row, i) => {
      const input = el("input", { value: row.name, maxlength: 30, "aria-label": `메뉴 ${i + 1}` });
      input.addEventListener("input", () => (row.name = input.value));
      return el("li", {},
        input,
        el("span", { class: "tag" }, row.source === "neis" ? "나이스" : "직접"),
        el("button", { class: "btn icon", "aria-label": "위로", disabled: i === 0, onclick: () => { [rows[i - 1], rows[i]] = [rows[i], rows[i - 1]]; draw(); } }, "↑"),
        el("button", { class: "btn icon", "aria-label": "지우기", onclick: () => {
          if (row.reviewCount && !confirm(`이 메뉴에는 평가가 ${row.reviewCount}개 있어요. 저장하면 같이 지워져요. 뺄까요?`)) return;
          rows.splice(i, 1);
          draw();
        } }, "✕"),
      );
    }));
  }

  async function loadNeisStatus() {
    const { last, next, hour, days } = await safe(() => call("/api/teacher/neis-status"));
    const when = (iso) => new Date(iso).toLocaleString("ko-KR", { timeZone: "Asia/Seoul", month: "long", day: "numeric", hour: "numeric", minute: "2-digit" });
    neisBox.replaceChildren(
      el("strong", {}, "나이스 자동 불러오기"),
      el("p", { class: "hint" }, `서버가 매일 아침 ${hour}시에 지난 ${days}일 급식을 스스로 불러와요. 메뉴가 없는 날만 채우고, 선생님이 고친 메뉴는 그대로 둬요.`),
      last
        ? el("p", { class: last.ok ? "hint" : "error" }, `마지막: ${when(last.at)} · ${last.message}`)
        : el("p", { class: "hint" }, "아직 불러온 적이 없어요."),
      el("p", { class: "hint" }, `다음: ${when(next)}`),
    );
  }

  async function save() {
    const menus = rows
      .map((r) => ({ ...r, name: r.name.trim() }))
      .filter((r) => r.name)
      .map((r) => ({
        id: r.id ?? null,
        name: r.name,
        source: r.source === "neis" && r.original === r.name ? "neis" : "manual",
      }));
    try {
      await safe(() => call(`/api/teacher/menus/${dateInput.value}`, { method: "PUT", body: { menus } }));
      toast("메뉴를 저장했어요!");
      await load();
    } catch { /* safe() 가 알려줬어요 */ }
  }

  dateInput.addEventListener("change", load);
  box.replaceChildren(
    el("h2", {}, "메뉴 확인 · 고치기"),
    neisBox,
    el("div", { class: "form-row" }, dateInput),
    status,
    listEl,
    el("div", { class: "form-row" },
      el("button", { class: "btn ghost", onclick: () => { rows.push({ name: "", source: "manual" }); draw(); listEl.querySelector("li:last-child input")?.focus(); } }, "+ 메뉴 추가"),
      el("button", { class: "btn primary", onclick: save }, "저장"),
    ),
  );
  load().catch(() => {});
  loadNeisStatus().catch(() => {});
}

// ---------- 2. 후기 관리 ----------

const HIDDEN_BY = { reports: "신고 3회로 자동 숨김", teacher: "선생님이 숨김" };

function renderReviews(box) {
  const mode = el("select", { "aria-label": "보기" },
    el("option", { value: "flagged" }, "신고되었거나 숨긴 후기"),
    el("option", { value: "date" }, "날짜별 전체 후기"),
  );
  const dateInput = el("input", { type: "date", value: todayKst(), "aria-label": "날짜", hidden: true });
  const listEl = el("div");
  const wordsBox = el("div");

  async function load() {
    dateInput.hidden = mode.value !== "date";
    listEl.replaceChildren(el("p", { class: "loading" }, "불러오는 중…"));
    const q = mode.value === "date" ? `mode=date&date=${dateInput.value}` : "mode=flagged";
    const { reviews } = await safe(() => call(`/api/teacher/reviews?${q}`));

    if (!reviews.length) {
      listEl.replaceChildren(el("p", { class: "empty small" }, mode.value === "date" ? "이날 후기가 없어요." : "신고되었거나 숨긴 후기가 없어요. 👍"));
      return;
    }
    const summary = mode.value === "date"
      ? el("p", { class: "hint" }, `${reviews.length}개 평가 · 평균 ★ ${average(reviews.map((r) => r.stars)).toFixed(1)}`)
      : "";
    listEl.replaceChildren(summary, el("ul", { class: "reviews" }, reviews.map((r) => el("li", { class: r.hidden ? "is-hidden" : "" },
      el("div", { class: "review-head" },
        el("strong", {}, `${r.date} · ${r.menu}`),
        el("span", { class: "review-stars" }, starText(r.stars)),
      ),
      el("p", {}, r.comment || "(별점만)"),
      el("p", { class: "hint" },
        `${r.nickname} · 신고 ${r.report_count}회`,
        r.hidden ? ` · ${HIDDEN_BY[r.hidden_by] || "숨김"}` : ""),
      el("div", { class: "form-row" },
        r.hidden
          ? el("button", { class: "btn", onclick: () => setHidden(r, false) }, "되살리기")
          : el("button", { class: "btn", onclick: () => setHidden(r, true) }, "숨기기"),
        el("button", { class: "btn ghost", onclick: () => remove(r) }, "지우기"),
      ),
    ))));
  }

  async function setHidden(r, hidden) {
    await safe(() => call(`/api/teacher/reviews/${r.id}`, { method: "PATCH", body: { hidden } }));
    toast(hidden ? "숨겼어요." : "되살렸어요.");
    load();
  }

  async function remove(r) {
    if (!confirm("이 후기를 완전히 지울까요? 되돌릴 수 없어요.")) return;
    await safe(() => call(`/api/teacher/reviews/${r.id}`, { method: "DELETE" }));
    toast("지웠어요.");
    load();
  }

  function drawWords(words) {
    const input = el("input", { placeholder: "추가할 금지어", maxlength: 20, "aria-label": "추가할 금지어" });
    wordsBox.replaceChildren(
      el("h3", {}, "추가 금지어"),
      el("p", { class: "hint" }, "기본 욕설 목록은 이미 들어 있어요. 우리 반에서 더 막고 싶은 말을 넣어 주세요 (친구 이름으로 놀리는 말 등)."),
      el("div", { class: "chips" }, words.length ? words.map((w) =>
        el("span", { class: "chip" }, w, el("button", { class: "btn link", "aria-label": `${w} 빼기`, onclick: async () => {
          const res = await safe(() => call(`/api/teacher/banned-words/${encodeURIComponent(w)}`, { method: "DELETE" }));
          drawWords(res.words);
        } }, "✕"))) : el("span", { class: "hint" }, "아직 없어요.")),
      el("form", { class: "form-row", onsubmit: async (e) => {
        e.preventDefault();
        const word = input.value.trim();
        if (!word) return;
        const res = await safe(() => call("/api/teacher/banned-words", { method: "POST", body: { word } }));
        drawWords(res.words);
      } }, input, el("button", { class: "btn", type: "submit" }, "추가")),
    );
  }

  mode.addEventListener("change", () => load().catch(() => {}));
  dateInput.addEventListener("change", () => load().catch(() => {}));
  box.replaceChildren(
    el("h2", {}, "후기 관리"),
    el("div", { class: "form-row" }, mode, dateInput),
    listEl,
    el("hr"),
    wordsBox,
  );
  load().catch(() => {});
  safe(() => call("/api/teacher/banned-words")).then((res) => drawWords(res.words), () => {});
}

// ---------- 3. 학생 · 입장 코드 ----------

function renderClass(box) {
  const codeBox = el("div");
  const listEl = el("div");

  function drawCode(code) {
    codeBox.replaceChildren(
      el("h3", {}, "반 입장 코드"),
      el("p", { class: "code" }, code || "아직 없어요"),
      el("p", { class: "hint" }, "칠판에 써 주세요. 새 코드를 만들면 이미 들어온 친구는 그대로이고, 새로 들어올 친구만 새 코드를 써야 해요."),
      el("button", { class: "btn", onclick: async () => {
        if (code && !confirm("새 입장 코드를 만들까요?")) return;
        const res = await safe(() => call("/api/teacher/join-code", { method: "POST" }));
        drawCode(res.code);
      } }, code ? "새 코드 만들기" : "코드 만들기"),
    );
  }

  async function loadStudents() {
    const { students } = await safe(() => call("/api/teacher/students"));
    listEl.replaceChildren(
      el("h3", {}, `들어온 학생 ${students.length}명`),
      el("p", { class: "hint" }, "닉네임에 진짜 이름이 들어가 있으면 바꿔 주세요."),
      students.length === 0 ? el("p", { class: "empty small" }, "아직 아무도 안 들어왔어요.") :
      el("ul", { class: "students" }, students.map((s) => el("li", {},
        el("span", {}, s.nickname),
        el("button", { class: "btn", onclick: async () => {
          const next = (prompt("새 닉네임 (10자 이내)", s.nickname) || "").trim();
          if (!next || next === s.nickname) return;
          await safe(() => call(`/api/teacher/students/${s.id}`, { method: "PATCH", body: { nickname: next } }));
          toast("바꿨어요.");
          loadStudents();
        } }, "닉네임 바꾸기"),
        el("button", { class: "btn ghost", onclick: async () => {
          if (!confirm(`${s.nickname} 학생을 내보낼까요? 이 학생의 별점과 후기도 지워지고, 다시 입장 코드를 넣어야 해요.`)) return;
          await safe(() => call(`/api/teacher/students/${s.id}`, { method: "DELETE" }));
          loadStudents();
        } }, "내보내기"),
      ))),
    );
  }

  box.replaceChildren(el("h2", {}, "학생 · 입장 코드"), codeBox, el("hr"), listEl);
  safe(() => call("/api/teacher/join-code")).then((res) => drawCode(res.code), () => {});
  loadStudents().catch(() => {});
}
