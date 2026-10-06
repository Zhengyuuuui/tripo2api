#!/usr/bin/env python3
"""
tripo2api 账号保活守护（Kratos session + ES256 JWT 双层）

后台线程独立于浏览器工作，做四件事：
  1. JWT 提前续签      —— 每 RENEW_INTERVAL 秒检查，剩余 < RENEW_AHEAD 时提前换新，
                         避免"首次请求要等一次 401 往返"
  2. 额度快照刷新      —— 每 CREDIT_INTERVAL 秒拉一次 credits / quota / 资产数，
                         顺带探测 session 是否还有效
  3. session 临期预警  —— 剩余 < WARN_DAYS 天时写 DB + 日志 + 回调标记
  4. 故障退避          —— 连续失败进入指数退避，避免把账号打到风控

关于"RT"：Tripo 用的是 Ory Kratos，不是 access/refresh 双 token 架构。
  - ory_kratos_session cookie  ≈ 长期凭证（30 天，不可续，只能重新登录）
  - ES256 JWT                 ≈ access token（15 分钟，可无限续签）
所以"JWT 续签"可以全自动；"session 续命"不行，只能预警。

环境变量：
  TRIPO_KEEPALIVE   1/0     是否启用（默认 1）
  TRIPO_RENEW_SEC   轮询间隔秒（默认 300）
  TRIPO_CREDIT_SEC  额度刷新间隔秒（默认 1800）
  TRIPO_WARN_DAYS   预警阈值天（默认 3）
"""
import json, os, sys, threading, time

import db

RENEW_INTERVAL = int(os.environ.get("TRIPO_RENEW_SEC", "300"))       # JWT 检查间隔
RENEW_AHEAD = int(os.environ.get("TRIPO_RENEW_AHEAD_SEC", "300"))   # 剩余不足则续
CREDIT_INTERVAL = int(os.environ.get("TRIPO_CREDIT_SEC", "1800"))  # 额度刷新间隔
WARN_DAYS = float(os.environ.get("TRIPO_WARN_DAYS", "3"))
MAX_BACKOFF = 300


def _log(msg):
    sys.stderr.write("[keepalive] %s\n" % msg)
    sys.stderr.flush()


def jwt_left(a):
    """JWT 剩余秒数；无法解析返回 None。"""
    t = (a or {}).get("jwt") or ""
    if t.count(".") != 2:
        return None
    try:
        import base64
        p = t.split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p).decode()).get("exp", 0)) - int(time.time())
    except Exception:
        return None


def session_left(acct, refresh):
    """Kratos session 剩余秒数（凭 whoami 的 expires_at）；取不到返回 None。"""
    info = refresh(acct) or {}
    exp = info.get("expires_at")
    if not exp:
        return None
    try:
        # 2026-11-04T03:20:28.16759Z
        import datetime
        s = exp.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int((dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
    except Exception:
        return None


class KeepAlive:
    def __init__(self, load_pool, refresh_jwt, probe_account):
        self.load_pool = load_pool
        self.refresh_jwt = refresh_jwt
        self.probe_account = probe_account
        self.stop_flag = threading.Event()
        self.thread = None
        self.state = {          # sub -> {"fails": n, "next_ok_at": ts, "last_error": str}
        }
        self.last_credit_pull = 0.0
        self.started_at = time.time()
        self.runs = 0

    # ---------- 单账号动作 ----------
    def renew_one(self, a):
        """JWT 剩余不足则提前续签。返回 (是否续签, 错误信息)。"""
        left = jwt_left(a)
        if left is None:
            # JWT 缺失/损坏，直接续
            new = self.refresh_jwt(a)
            if new:
                a["jwt"] = new
                db.upsert_account(a)
                return True, None
            return False, "续签失败(无有效 session)"
        if left > RENEW_AHEAD:
            return False, None
        new = self.refresh_jwt(a)
        if not new:
            return False, "续签失败(剩余 %.0fs)" % left
        a["jwt"] = new
        db.upsert_account(a)
        return True, None

    def credit_one(self, a):
        """刷新额度快照 + 顺带探测 session。"""
        info = self.probe_account(a) or {}
        info["sub"] = a.get("sub")
        info.pop("cookie", None)
        info.pop("csrf", None)
        info.pop("jwt", None)
        # 不覆盖凭证
        db.upsert_account(a)
        db.upsert_account(info)
        return info

    # ---------- 主循环 ----------
    def tick(self):
        self.runs += 1
        pool = self.load_pool()
        if not pool:
            return
        now = time.time()
        do_credit = (now - self.last_credit_pull) >= CREDIT_INTERVAL

        for a in pool:
            key = a.get("sub") or a.get("email") or "?"
            st = self.state.setdefault(key, {"fails": 0, "next_ok_at": 0, "last_error": None})
            if now < st["next_ok_at"]:          # 退避中
                continue

            renewed = err = None
            try:
                renewed, err = self.renew_one(a)
                if err:
                    raise RuntimeError(err)
                if do_credit:
                    info = self.credit_one(a)
                    if not info.get("ok"):
                        raise RuntimeError("额度探测 HTTP %s" % info.get("status"))
                st["fails"] = 0
                st["next_ok_at"] = 0
                st["last_error"] = None
                if renewed:
                    _log("JWT 续签 %s" % (a.get("email") or key[:12]))
            except Exception as e:
                st["fails"] += 1
                st["last_error"] = str(e)
                st["next_ok_at"] = now + min(MAX_BACKOFF, 10 * (2 ** min(st["fails"], 5)))
                _log("保活失败 %s: %s (第%d次, 退避 %.0fs)"
                     % (a.get("email") or key[:12], e, st["fails"], st["next_ok_at"] - now))

        if do_credit:
            self.last_credit_pull = now

    def loop(self):
        _log("保活守护启动: JWT检查/%ds(剩余<%ds续) 额度/%ds 预警<%.0f天"
             % (RENEW_INTERVAL, RENEW_AHEAD, CREDIT_INTERVAL, WARN_DAYS))
        while not self.stop_flag.is_set():
            try:
                self.tick()
            except Exception as e:
                _log("tick 异常: %s" % e)
            self.stop_flag.wait(RENEW_INTERVAL)
        _log("保活守护退出")

    def start(self):
        if os.environ.get("TRIPO_KEEPALIVE", "1") not in ("1", "true", "yes"):
            _log("已通过 TRIPO_KEEPALIVE=0 禁用")
            return None
        if self.thread and self.thread.is_alive():
            return self.thread
        self.thread = threading.Thread(target=self.loop, name="tripo-keepalive", daemon=True)
        self.thread.start()
        return self.thread

    def stop(self):
        self.stop_flag.set()

    def status(self):
        return {
            "enabled": bool(self.thread and self.thread.is_alive()),
            "runs": self.runs,
            "uptime_s": int(time.time() - self.started_at),
            "jwt_interval_s": RENEW_INTERVAL,
            "jwt_ahead_s": RENEW_AHEAD,
            "credit_interval_s": CREDIT_INTERVAL,
            "warn_days": WARN_DAYS,
            "accounts": [{
                "sub": k,
                "fails": v["fails"],
                "backoff_left_s": max(0, int(v["next_ok_at"] - time.time())),
                "last_error": v["last_error"],
                "jwt_left_s": jwt_left(next((a for a in self.load_pool()
                                              if (a.get("sub") or a.get("email")) == k), {})),
            } for k, v in self.state.items()],
        }