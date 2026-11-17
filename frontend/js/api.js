// 백엔드(FastAPI) 부르기
const KEYS = { student: "lunch.studentToken", teacher: "lunch.teacherToken" };

export function getToken(role) {
  try { return localStorage.getItem(KEYS[role]); } catch { return null; }
}
export function setToken(role, token) {
  try {
    if (token) localStorage.setItem(KEYS[role], token);
    else localStorage.removeItem(KEYS[role]);
  } catch { /* 저장소를 못 쓰면 이번 창에서만 기억해요 */ }
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/**
 * api("student", "/api/me")
 * api("teacher", "/api/teacher/menus/2026-11-10", { method: "PUT", body: {...} })
 */
export async function api(role, path, { method = "GET", body } = {}) {
  const headers = {};
  const token = role && getToken(role);
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError("서버에 연결하지 못했어요. 서버가 켜져 있는지 확인해 주세요.", 0);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // FastAPI 는 {detail: "문장"} 또는 {detail: [검사 오류...]} 로 보내요.
    const message = typeof data.detail === "string" ? data.detail : "입력한 값을 다시 확인해 주세요.";
    throw new ApiError(message, res.status);
  }
  return data;
}
