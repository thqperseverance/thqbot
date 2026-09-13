# fetch_ai_news.py
import json
import os
import re

import requests

from ithqbot.utils.http_client import classify_http_client_error

# =========================
# 配置区
# =========================
WEBHOOK_URL = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
DAILY_NEWS_COUNT = 5   # 每日要闻条数
WEEKLY_HOT_COUNT = 10  # 每周热点排行榜条数，0为全部

URL = "https://news.aibase.com/zh/news"
HOMEPAGE_URL = "https://www.aibase.com/zh"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}


# =========================
# 功能函数
# =========================
def fetch_html(url):
    """获取网页 HTML"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        return resp.text
    except requests.RequestException as exc:
        error = classify_http_client_error("AI 新闻抓取", exc)
        raise RuntimeError(error.message) from exc


def extract_news_data():
    """提取页面里的新闻数据和热点排行榜"""
    # 提取最新新闻
    latest_news = []
    try:
        news_html = fetch_html(URL)
        # 模式: "标题", "图片URL", "描述", news_id, "日期", pv, {...}
        pattern = re.compile(r'"([^"]+)"\s*,\s*"https://[^"]+"\s*,\s*"[^"]*"\s*,\s*(\d+)\s*,\s*"2026-', re.S)
        matches = pattern.findall(news_html)
        
        for match in matches:
            title = match[0].strip().strip('"')
            news_id = match[1]
            if title and len(title) > 10 and not title.startswith('http'):
                latest_news.append({"name": title, "oid": news_id})
    except Exception as e:
        print(f"提取新闻数据失败：{e}")
    
    # 如果没有找到新闻数据，返回空列表
    if not latest_news:
        print("!!!未提取到新闻数据")
    
    # 动态提取热点排行榜
    hot_news = []
    try:
        homepage_html = fetch_html(HOMEPAGE_URL)
        # 尝试提取热点排行榜
        # 模式1: 匹配热点排行榜区域
        hot_pattern = re.compile(r'本周AI热点排行榜[\s\S]*?<ul[\s\S]*?</ul>', re.S)
        hot_match = hot_pattern.search(homepage_html)
        if hot_match:
            hot_html = hot_match.group(0)
            # 提取每条热点新闻
            item_pattern = re.compile(r'<li[\s\S]*?<a[^>]*?>([\s\S]*?)</a>[\s\S]*?</li>', re.S)
            items = item_pattern.findall(hot_html)
            for item in items:
                title = item.strip()
                if title and len(title) > 10:
                    # 尝试从链接中提取新闻ID
                    link_pattern = re.compile(r'href="([^"]+)"', re.S)
                    link_match = link_pattern.search(item)
                    oid = ""
                    if link_match:
                        link = link_match.group(1)
                        # 从链接中提取ID
                        id_pattern = re.compile(r'/(\d+)$')
                        id_match = id_pattern.search(link)
                        if id_match:
                            oid = id_match.group(1)
                    hot_news.append({"name": title, "oid": oid})
    except Exception as e:
        print(f"提取热点排行榜失败：{e}")
    
    # 如果没有提取到热点排行榜，使用AI每日要闻中top10的后五条
    if not hot_news:
        if len(latest_news) >= 10:
            hot_news = latest_news[5:10]  # 取后五条
        elif len(latest_news) >= 5:
            hot_news = latest_news[0:5]  # 如果不够10条，取前五条
        else:
            # 如果不够5条，使用所有可用的新闻
            hot_news = latest_news
    
    return latest_news, hot_news


def parse_daily_news(news_json, count=5):
    """生成每日要闻列表"""
    daily_news = []
    for i, item in enumerate(news_json[:count], 1):
        title = item.get("name") or "未命名"
        oid = item.get("oid")
        if oid:
            url = f"https://news.aibase.com/zh/news/{oid}"
            daily_news.append(f"{i}. [{title}]({url})")
        else:
            daily_news.append(f"{i}. {title}")
    return daily_news


def parse_weekly_hot(news_json, count=0):
    """生成每周热点列表"""
    if count > 0:
        news_json = news_json[:count]
    weekly_hot = []
    for i, item in enumerate(news_json, 1):
        title = item.get("name") or "未命名"
        oid = item.get("oid")
        if oid:
            url = f"https://news.aibase.com/zh/news/{oid}"
            weekly_hot.append(f"{i}. [{title}]({url})")
        else:
            weekly_hot.append(f"{i}. {title}")
    return weekly_hot


def generate_markdown_report(daily_news, weekly_hot):
    """生成 Markdown 格式报告"""
    report = "### AI 每日要闻（Top 5）\n"
    report += "\n".join(daily_news) + "\n\n"
    report += "### 本周AI热点排行榜\n"
    report += "\n".join(weekly_hot) if weekly_hot else "暂无排行榜数据"
    return report


def send_to_wechat(webhook, markdown_text):
    """发送 Markdown 消息到企业微信机器人"""
    if not webhook:
        print("!!!未配置企业微信 webhook，跳过发送")
        return
    data = {
        "msgtype": "markdown",
        "markdown": {"content": markdown_text}
    }
    try:
        res = requests.post(
            webhook,
            data=json.dumps(data),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        res.raise_for_status()
        print("消息发送成功")
    except requests.RequestException as exc:
        error = classify_http_client_error("企业微信推送", exc)
        print(f"!!!{error.message}")


# =========================
# 主流程
# =========================
def main():
    try:
        latest_news, hot_news = extract_news_data()
        
        if not latest_news:
            print("!!!未提取到新闻数据")
            return

        daily_news = parse_daily_news(latest_news, DAILY_NEWS_COUNT)
        weekly_hot = parse_weekly_hot(hot_news, WEEKLY_HOT_COUNT)

        report = generate_markdown_report(daily_news, weekly_hot)
        print(report)
        send_to_wechat(WEBHOOK_URL, report)

    except Exception as e:
        print("脚本执行失败：", e)


if __name__ == "__main__":
    main()
