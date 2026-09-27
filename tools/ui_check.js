#!/usr/bin/env node
/**
 * 控制台前端渲染自检（无需浏览器）
 *
 * 用 Node 的 vm 在假 DOM 里跑 static/index.html 里的脚本，喂入真实的 /api/state 与
 * /api/history 响应，验证：凭据有效期列、Token 过期提示条、历史按用户分组、
 * 「今日已签到」不计入积分、按钮改名 / 按用户重置、账户列表按平台排序等渲染逻辑。
 *
 * 用法（先让服务跑起来，或直接抓线上）：
 *   node tools/ui_check.js static/index.html state.json hist.json
 *
 * 抓线上数据：
 *   curl -s http://192.168.x.x:8080/api/state            -o state.json
 *   curl -s "http://192.168.x.x:8080/api/history?days=30" -o hist.json
 *   curl -s http://192.168.x.x:8080/                      -o index.html
 *   node tools/ui_check.js index.html state.json hist.json
 *
 * 退出码 0 = 全部通过，1 = 有断言失败。
 */
const fs = require("fs");
const vm = require("vm");

const [htmlPath, statePath, histPath] = process.argv.slice(2);
if (!htmlPath || !statePath || !histPath) {
  console.error("用法: node tools/ui_check.js <index.html> <state.json> <history.json>");
  process.exit(2);
}

const html = fs.readFileSync(htmlPath, "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const stateData = JSON.parse(fs.readFileSync(statePath, "utf8"));
const histData = JSON.parse(fs.readFileSync(histPath, "utf8"));

function makeEl(id) {
  return {
    id, innerHTML: "", textContent: "", value: "", checked: false, className: "",
    dataset: {}, style: {}, appendChild() {}, remove() {}, click() {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, force) {
        const on = force === undefined ? !this._s.has(c) : !!force;
        if (on) this._s.add(c); else this._s.delete(c);
        return on;
      },
    },
  };
}

const els = {};
const calls = [];               // 记录前端发出的请求，便于断言「重置」按钮打对了接口
const sandbox = {
  document: {
    getElementById: (id) => (els[id] = els[id] || makeEl(id)),
    querySelectorAll: () => [],
    querySelector: () => makeEl("q"),
    createElement: () => makeEl("new"),
  },
  console,
  setInterval: () => 0,
  setTimeout: () => 0,
  confirm: () => true,
  alert: () => {},
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  Blob: class { constructor(parts) { this.parts = parts; } },
  fetch: async (url, opts) => {
    const u = String(url);
    calls.push([u, (opts && opts.method) || "GET", (opts && opts.body) || ""]);
    if (u.startsWith("/api/state")) return { status: 200, ok: true, json: async () => stateData };
    if (u.startsWith("/api/history/reset")) {
      return { status: 200, ok: true, json: async () => ({ ok: true, removed: 3, group: "x" }) };
    }
    if (u.startsWith("/api/history")) return { status: 200, ok: true, json: async () => histData };
    return { status: 200, ok: true, json: async () => ({}) };
  },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
const run = (code) => vm.runInContext(code, sandbox, { filename: "chunk.js" });
run(script);

let fail = 0;
const check = (label, cond, extra) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}`);
  if (!cond) { fail++; if (extra !== undefined) console.log("      ", extra); }
};

(async () => {
  // 让脚本末尾的 refreshAll(true) 的 promise 链跑完（setImmediate 在微任务之后执行）
  await new Promise((r) => setImmediate(r));
  run("stateCache = " + JSON.stringify(stateData) + "; renderState();");

  // 历史可能被「重置」清空（这是合法状态）——此时用合成数据继续验证用户表与重置按钮的接线，
  // 否则空数据会让断言恒假，看不出真实问题
  let H = histData;
  if (!(histData.records || []).length && stateData.accounts.length) {
    const a = stateData.accounts[0];
    const key = `${a.platform}|${a.user_id || a.name}`;
    const rec = {
      ts: Math.floor(Date.now() / 1000), date: "2026-09-28", time: "00:05",
      platform: a.platform, name: a.name, user_key: a.user_id, username: a.nickname,
      ok: true, claimed: true, credits: 100, message: "签到成功", group: key,
    };
    H = {
      records: [rec],
      users: [{
        key, name: a.name, platform: a.platform, nickname: a.nickname,
        total: 1, ok: 1, failed: 0, days: 1, credit_days: 1, credits: 100,
        first_date: "2026-09-28", last_date: "2026-09-28", last_time: "00:05",
        last_ts: rec.ts, last_ok: true, last_message: "签到成功",
      }],
      summary: { total: 1, ok: 1, failed: 0, credits: 100, days: 1, claimed_days: 1, users: 1 },
    };
    console.log("[提示] 线上历史为空（已被重置），改用合成数据验证用户表 / 重置按钮接线");
  }

  // ---------- 1. 凭据有效期列 ----------
  const cells = run("stateCache.accounts.map(a => expiresCell(a))");
  console.log("\n[凭据有效期列]");
  stateData.accounts.forEach((a, i) =>
    console.log("  -", a.platform, a.nickname || a.name, "=>", String(cells[i]).replace(/\s+/g, " ")));

  const expiredIdx = stateData.accounts.findIndex((a) => a.expired && !a.auto_renew);
  if (expiredIdx >= 0) {
    check("过期且不可续期时提示运行同步脚本", cells[expiredIdx].includes("同步登录信息到NAS.bat"));
  }
  check("有到期时间的账户都显示了日期",
    stateData.accounts.every((a, i) => !a.has_expiry || String(cells[i]).includes(a.expires_label)));

  // ---------- 2. 顶部告警条 ----------
  const fake = JSON.parse(JSON.stringify(stateData));
  fake.accounts[0].expired = true;
  fake.accounts[0].auto_renew = false;
  fake.accounts[0].expires_label = "2026-01-01 08:00";
  fake.accounts[0].expires_in_days = -100;
  run("stateCache = " + JSON.stringify(fake) + "; renderState();");
  const hard = els["token-alert"].innerHTML;
  console.log("\n[硬过期告警]", els["token-alert"].className, "|", hard.replace(/\s+/g, " ").slice(0, 200));
  check("硬过期 → alert-err", els["token-alert"].className.includes("alert-err"));
  check("硬过期 → 文案含「同步登录信息到NAS.bat」", hard.includes("同步登录信息到NAS.bat"));
  check("硬过期 → 单元格标红已过期", els["accounts-body"].innerHTML.includes("已过期"));

  // ---------- 3. 历史按用户分组 ----------
  run("stateCache = " + JSON.stringify(stateData) + ";");
  run("historyCache = " + JSON.stringify(H.records) + "; historyUsers = "
    + JSON.stringify(H.users) + "; expandedUsers = new Set(); renderHistory("
    + JSON.stringify(H.summary) + ");");

  const userHtml = els["hist-user-body"].innerHTML;
  console.log("\n[按用户表格]");
  check("每个用户一行", H.users.every((u) => userHtml.includes(`toggleUser('${u.key}')`)));
  check("默认不展开明细", !userHtml.includes("child-table"));
  check("汇总卡含「累计获得积分」", els["history-summary"].innerHTML.includes("累计获得积分"));

  const firstKey = H.users[0] && H.users[0].key;
  if (firstKey) {
    run(`toggleUser(${JSON.stringify(firstKey)});`);
    const expanded = els["hist-user-body"].innerHTML;
    const mine = H.records.filter((r) => r.group === firstKey);
    console.log("\n[展开首个用户]", firstKey, "明细条数:", mine.length);
    check("展开后出现子表", expanded.includes("child-table"));
    check("「今日已签到」标为不计入",
      !mine.some((r) => r.ok && !r.claimed) || expanded.includes("不计入"));
  }
  run("renderDetailRows();");
  check("明细视图渲染", els["history-body"].innerHTML.includes("<td"));

  // ---------- 4. 积分口径一致性 ----------
  const perUser = H.users.reduce((s, u) => s + (u.credits || 0), 0);
  check(`各用户积分之和 = 汇总(${perUser} vs ${H.summary.credits})`,
    Math.abs(perUser - H.summary.credits) < 1e-6);
  check("claimed_days ≤ days",
    (H.summary.claimed_days || 0) <= (H.summary.days || 0));

  // ---------- 5. 老记录 claimed 推断 ----------
  run(`__c__ = [recordClaimed({ok:true,message:"今日已签到"}), recordClaimed({ok:true,message:"签到成功"}),
        recordClaimed({ok:false,message:"x"}), recordClaimed({ok:true,claimed:false,credits:150,message:"签到成功"}),
        recordClaimed({ok:true,claimed:true,credits:150,message:"签到成功"})];`);
  const c = run("__c__");
  console.log("\n[老记录 claimed 推断]", JSON.stringify(c));
  check("老「今日已签到」不计入 / 「签到成功」计入", c[0] === false && c[1] === true && c[2] === false);
  check("新记录以 claimed 字段为准", c[3] === false && c[4] === true);

  // ---------- 6. 按钮名称 / 按用户重置历史 ----------
  run("stateCache = " + JSON.stringify(stateData) + "; renderState();");
  const accHtml = els["accounts-body"].innerHTML;
  console.log("\n[账户操作列]");
  check("「状态」按钮已改名为「更新状态」", accHtml.includes(">更新状态<"));
  check("不再出现旧名「状态」按钮", !/>状态</.test(accHtml));

  const u0 = H.users[0];
  console.log("\n[重置某用户历史]");
  if (u0) {
    check("用户行有「重置」按钮", userHtml.includes(`resetUserHistory('${u0.key}'`));
    calls.length = 0;
    run(`resetUserHistory(${JSON.stringify(u0.key)}, "测试用户", 3);`);
    const hit = calls.find((c) => c[0] === "/api/history/reset");
    check("重置按钮调用 POST /api/history/reset", !!hit && hit[1] === "POST");
    let sent = {};
    try { sent = JSON.parse(hit[2]); } catch (e) { /* ignore */ }
    check("请求体带上了该用户的分组键", sent.group === u0.key, JSON.stringify(sent));
  }

  // ---------- 7. 账户列表按平台排序 ----------
  console.log("\n[账户排序]");
  const platRank = (p) => ({ trae: 0, workbuddy: 1 }[String(p || "").toLowerCase()] ?? 99);
  const ranks = stateData.accounts.map((a) => platRank(a.platform));
  check("平台顺序非递减（TRAE 一组在前，WorkBuddy 一组在后）",
    ranks.every((r, i) => i === 0 || ranks[i - 1] <= r),
    JSON.stringify(stateData.accounts.map((a) => [a.platform, a.nickname || a.name])));
  const uniqAll = [...new Set(ranks)];
  const uniqAdj = ranks.filter((r, i) => i === 0 || ranks[i - 1] !== r);
  check("按平台归类（同一平台连续，不交错）",
    JSON.stringify(uniqAdj) === JSON.stringify(uniqAll), JSON.stringify({ ranks, uniqAdj }));
  const ident = (a) => String(a.username || a.nickname || a.user_id || a.name || "").toLowerCase();
  const groups = {};                       // 平台 rank -> 该平台的账户
  stateData.accounts.forEach((a, i) => (groups[ranks[i]] = groups[ranks[i]] || []).push(a));
  const sortedInside = Object.values(groups).every((g) => {
    const ids = g.map(ident);
    return ids.every((v, i) => i === 0 || ids[i - 1] <= v);
  });
  check("同平台内按昵称 / UID 排序（顺序稳定可预期）", sortedInside,
    JSON.stringify(Object.values(groups).map((g) => g.map(ident))));
  const dropdown = (() => {
    // 下拉框由 renderState() → fillHistoryAccounts() 填充，这里强制再跑一次保证是最新数据
    run("fillHistoryAccounts();");
    return els["hist-account"].innerHTML;
  })();
  const dropdownOrder = stateData.accounts
    .map((a) => dropdown.indexOf(`value="${a.name}"`))
    .filter((i) => i >= 0);
  check("历史页账户下拉顺序与账户表一致",
    dropdownOrder.every((v, i) => i === 0 || dropdownOrder[i - 1] <= v), JSON.stringify(dropdownOrder));

  console.log(fail ? `\n${fail} 项未通过` : "\n全部通过");
  process.exit(fail ? 1 : 0);
})();
