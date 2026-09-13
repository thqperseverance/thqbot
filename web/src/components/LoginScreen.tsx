import { useState } from "react";
import type { FormEvent } from "react";

import { ApiError, api } from "../api";
import type { User } from "../types";
import { SparkIcon } from "./Icons";

export default function LoginScreen({ onSuccess }: { onSuccess: (user: User) => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSuccess(await api.login(username.trim(), password));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <span className="brand-mark">
            <SparkIcon size={13} />
          </span>
          <div>
            <h1>thqbot</h1>
            <p>Agent 平台 MVP</p>
          </div>
        </div>

        <label className="field">
          <span>用户名</span>
          <input
            value={username}
            autoComplete="username"
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>

        <label className="field">
          <span>密码</span>
          <input
            type="password"
            value={password}
            autoComplete="current-password"
            placeholder="默认 admin123456"
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>

        {error && <div className="login-error">{error}</div>}

        <button
          className="login-submit"
          type="submit"
          disabled={busy || !username.trim() || !password}
        >
          {busy ? "登录中…" : "进入工作台"}
        </button>
      </form>
    </div>
  );
}
