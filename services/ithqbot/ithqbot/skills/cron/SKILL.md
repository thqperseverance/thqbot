---
name: cron
description: 创建提醒、定时任务和周期任务；当用户要求稍后提醒、定时执行或按周期跟进时使用。
---

# 定时任务

## 目标

使用 `cron` 工具安排提醒、一次性任务或周期执行任务。

## 何时使用

- “20 分钟后提醒我”
- “每天早上 9 点提醒我开会”
- “每小时检查一次某个指标并汇报”
- “下周一上午 10 点执行一次这个任务”

## 输入约束

- `action` 必填，仅支持 `add`、`list`、`remove`
- 新增任务时，至少提供 `message` 与一种时间表达：`every_seconds`、`cron_expr` 或 `at`
- 删除任务时必须提供 `job_id`
- 如用户指定时区，优先传 `tz`，且应使用 IANA 时区名称

## 模式

1. **Reminder**：只发送提醒消息
2. **Task**：把 `message` 当作任务描述，由系统定时触发执行
3. **One-time**：在指定时间只执行一次，执行后自动删除

## 参数映射

| 用户表达 | 推荐参数 |
|-----------|------------|
| 每 20 分钟 | `every_seconds: 1200` |
| 每小时 | `every_seconds: 3600` |
| 每天 8 点 | `cron_expr: "0 8 * * *"` |
| 工作日 17 点 | `cron_expr: "0 17 * * 1-5"` |
| 温哥华时间每天 9 点 | `cron_expr: "0 9 * * *", tz: "America/Vancouver"` |
| 某个具体时间点 | `at: ISO datetime` |

## 执行步骤

1. 把自然语言时间转换为 `every_seconds`、`cron_expr` 或 `at`
2. 如用户指定时区，优先传 `tz`
3. 调用 `cron(action="add", ...)`
4. 如用户要查看或取消任务，使用 `list/remove`

## 输出约定

- `add` 成功时，应向用户确认任务内容、触发方式与时区
- `list` 成功时，应按任务 ID、时间规则和消息内容整理返回结果
- `remove` 成功时，应明确告知对应任务已取消
- 如果时间表达不完整或已过期，应先指出问题，再要求补足或改写参数

## 示例

固定提醒：

```
cron(action="add", message="Time to take a break!", every_seconds=1200)
```

动态任务：

```
cron(action="add", message="检查当前仓库的 GitHub Star 数并汇报", every_seconds=600)
```

一次性任务：

```
cron(action="add", message="Remind me about the meeting", at="<ISO datetime>")
```

带时区的周期任务：

```
cron(action="add", message="Morning standup", cron_expr="0 9 * * 1-5", tz="America/Vancouver")
```

查看与删除：

```
cron(action="list")
cron(action="remove", job_id="abc123")
```

## 风险与边界

- `at` 必须是未来时间，不能传过去的时间
- 没有明确时区时，默认使用服务端本地时区
- 若用户要“持续跟进某件事”，优先判断是使用 `cron` 还是写入 `HEARTBEAT.md`
