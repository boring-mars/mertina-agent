/**
 * All user-facing UI text, in one place so it can be translated later.
 *
 * Code and comments stay English; only these strings are Chinese.
 */
export const copy = {
  appName: "Mertina",
  tagline: "你的个人助手",
  gateway: {
    checking: "正在连接 Gateway…",
    online: "Gateway 已连接",
    offline: "未连接 Gateway",
    offlineHint: "请先运行 uv run mertina gateway，然后刷新页面。",
    retry: "重新连接",
  },
  scaffold: "界面正在开发中：会话与对话功能会在后续版本加入。",
} as const;
