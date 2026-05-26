import os
import sys
import requests
import asyncio
from datetime import datetime, timedelta, timezone
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright

# ================= 1. 配置区域 =================
ROCOM_API_KEY = os.environ.get("ROCOM_API_KEY")
IMGBB_KEY = os.environ.get("IMGBB_KEY")
NOTIFYME_UUID = os.environ.get("NOTIFYME_UUID")
BARK_KEY = os.environ.get("BARK_KEY")
UNICLOUD_URL = os.environ.get("UNICLOUD_URL")

GAME_API_URL = "https://wegame.shallow.ink/api/v1/games/rocom/merchant/info"
NOTIFYME_SERVER = "https://notifyme-server.wzn556.top/api/send"
ASSETS_DIR = os.path.abspath("assets/yuanxing-shangren")
HTML_TEMPLATE_FILE = "index.html"
TEMP_RENDER_FILE = "temp_render.html"

# 北京时间时区
BEIJING_TZ = timezone(timedelta(hours=8))

# 刷新时间点（北京时间小时）
REFRESH_HOURS = [8, 12, 16, 20]
# 每个小时内的执行分钟数（整点、5分、10分）
REFRESH_MINUTES = [0, 5, 10]

# GitHub Actions 事件类型
GITHUB_EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")
IS_MANUAL_TRIGGER = (GITHUB_EVENT_NAME == "workflow_dispatch")

# ================= 2. 时间守卫函数 =================

def get_beijing_time():
    return datetime.now(BEIJING_TZ)

def should_execute() -> bool:
    """
    判断当前时间是否在计划执行窗口内：
    计划执行时间 = 每个 (小时, 分钟) 组合
    允许前后 1 分钟的误差（防止调度器微小偏差）
    """
    now = get_beijing_time()
    # 构建所有计划执行时间点
    plan_times = []
    for h in REFRESH_HOURS:
        for m in REFRESH_MINUTES:
            plan_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            plan_times.append(plan_time)

    for pt in plan_times:
        # 允许前后 1 分钟
        if pt - timedelta(minutes=1) <= now <= pt + timedelta(minutes=1):
            print(f"✅ 时间守卫通过：当前北京时间 {now.strftime('%H:%M')} 在计划执行窗口内（{pt.strftime('%H:%M')} ±1min）")
            return True

    print(f"⏭️ 时间守卫拦截：当前北京时间 {now.strftime('%H:%M:%S')} 不在任何计划执行窗口内")
    return False

# ================= 3. 时间与数据处理逻辑 =================

def format_timestamp(ts_ms):
    if not ts_ms:
        return "--:--"
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=BEIJING_TZ)
    return dt.strftime("%H:%M")

def get_round_info():
    """计算当前远行商人的轮次与倒计时"""
    now = get_beijing_time()
    start_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
    
    if now < start_time:
        return {"current": "未开放", "total": 4, "countdown": "尚未开市"}
    
    delta_seconds = int((now - start_time).total_seconds())
    round_index = (delta_seconds // (4 * 3600)) + 1
    
    if round_index > 4:
        return {"current": 4, "total": 4, "countdown": "今日已收市"}
    
    round_end = start_time + timedelta(hours=round_index * 4)
    remaining = round_end - now
    hours, rem = divmod(int(remaining.total_seconds()), 3600)
    minutes, _ = divmod(rem, 60)
    countdown_str = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
    
    return {
        "current": round_index,
        "total": 4,
        "countdown": countdown_str
    }

def process_data_for_template(data):
    if not data:
        return {}
    
    now_ms = int(get_beijing_time().timestamp() * 1000)
    round_info = get_round_info()
    
    activities = data.get("merchantActivities") or data.get("merchant_activities") or []
    activity = activities[0] if activities else {}
    
    buckets = [
        ("道具", activity.get("get_props") or []),
        ("额外道具", activity.get("get_extra_props") or []),
        ("精灵", activity.get("get_pets") or []),
    ]

    random_goods = data.get("random_goods") if isinstance(data.get("random_goods"), list) else []
    goods_meta_by_name = {
        str(item.get("goods_name", "") or item.get("name", "")).strip(): item
        for item in random_goods
        if isinstance(item, dict) and str(item.get("goods_name", "") or item.get("name", "")).strip()
    }

    all_products = []
    active_products = []
    
    for category, items in buckets:
        for item in items:
            if not isinstance(item, dict):
                continue

            goods_meta = goods_meta_by_name.get(str(item.get("name", "")).strip(), {})
            
            s_time = item.get("start_time")
            e_time = item.get("end_time")

            if s_time is None:
                s_time = activity.get("start_time")
            if e_time is None:
                e_time = activity.get("end_time")

            start_ms = int(s_time) if s_time else None
            end_ms = int(e_time) if e_time else None

            is_active = True
            if start_ms is not None and end_ms is not None:
                is_active = start_ms <= now_ms < end_ms

            status_label = "当前轮次"
            if start_ms is not None and now_ms < start_ms:
                status_label = "未开始"
            elif end_ms is not None and now_ms >= end_ms:
                status_label = "已结束"

            start_str = format_timestamp(start_ms)
            end_str = format_timestamp(end_ms)
            if start_str[:5] == end_str[:5] and start_str != "--:--":
                time_label = f"{start_str} - {end_str[6:]}" if len(end_str) > 6 else f"{start_str} - {end_str}"
            else:
                time_label = f"{start_str} - {end_str}"

            # 获取价格和限购数量
            price = item.get("price")
            if price is None or price == "":
                price = goods_meta.get("price")
            buy_limit = item.get("buy_limit_num")
            if buy_limit is None or buy_limit == "":
                buy_limit = goods_meta.get("buy_limit_num")

            product = {
                "name": item.get("name", "未知商品"),
                "image": item.get("icon_url", ""),
                "time_label": time_label,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "is_active": is_active,
                "status_label": status_label,
                "price": price,
                "buy_limit_num": buy_limit
            }
            
            all_products.append(product)
            if is_active:
                active_products.append(product)
                
    # 历史记录分组逻辑（保持不变）
    today = datetime.fromtimestamp(now_ms / 1000, tz=BEIJING_TZ).strftime("%Y-%m-%d")
    grouped = {}
    for product in all_products:
        if product["is_active"]:
            continue
        start_ms = product["start_ms"]
        if not start_ms:
            continue
        start_dt = datetime.fromtimestamp(start_ms / 1000, tz=BEIJING_TZ)
        if start_dt.strftime("%Y-%m-%d") != today:
            continue
        key = f"{start_ms}-{product['end_ms'] or ''}"
        if key not in grouped:
            grouped[key] = {
                "time_label": product["time_label"] or "--:--",
                "status_label": product["status_label"] or "其他时段",
                "sort": start_ms,
                "products": []
            }
        group = grouped[key]
        names = {p["name"] for p in group["products"]}
        if product["name"] not in names and len(group["products"]) < 5:
            group["products"].append(product)

    history_groups = [
        {k: v for k, v in g.items() if k != "sort"}
        for g in sorted(grouped.values(), key=lambda x: x["sort"])
        if g["products"]
    ]
            
    return {
        "title": activity.get("name", "远行商人"),
        "subtitle": activity.get("start_date", "每日 08:00/08:05/08:10 / 12:00/12:05/12:10 / 16:00/16:05/16:10 / 20:00/20:05/20:10 刷新"),
        "product_count": len(active_products),
        "round_info": round_info,
        "products": active_products,
        "history_groups": history_groups,
        "_res_path": "",
        "background": "img/bg.C8CUoi7I.jpg",
        "titleIcon": True
    }

# ================= 4. 图像渲染与上传 =================

async def render_to_image(processed_data):
    """渲染 HTML 并精准切割截图"""
    if not processed_data or processed_data["product_count"] == 0:
        print("当前无活跃商品，跳过渲染")
        return None
    
    screenshot_file = "merchant_render.jpg"
    temp_html_path = os.path.join(ASSETS_DIR, TEMP_RENDER_FILE)
    
    try:
        env = Environment(loader=FileSystemLoader(ASSETS_DIR))
        template = env.get_template(HTML_TEMPLATE_FILE)
        rendered_html = template.render(processed_data)
        
        with open(temp_html_path, "w", encoding="utf-8") as f:
            f.write(rendered_html)
            
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_viewport_size({"width": 900, "height": 1600})
            await page.goto(f"file://{temp_html_path}")
            await page.evaluate("document.fonts.ready")
            await page.wait_for_load_state("networkidle")
            data_region = page.locator('.merchant-page')
            await data_region.screenshot(path=screenshot_file, type="jpeg", quality=90)
            await browser.close()
            print(f"✅ 图片渲染成功: {screenshot_file}")
            return screenshot_file
            
    except Exception as e:
        print(f"❌ 渲染图片失败: {e}")
        return None
    finally:
        if os.path.exists(temp_html_path):
            os.remove(temp_html_path)

async def upload_to_imgbb(image_path):
    """上传到 ImgBB 图床"""
    if not image_path or not IMGBB_KEY:
        return None
    try:
        with open(image_path, "rb") as f:
            res = requests.post("https://api.imgbb.com/1/upload", data={"key": IMGBB_KEY}, files={"image": f}, timeout=30)
            json_data = res.json()
            if json_data.get("status") == 200:
                print("✅ 图床上传成功")
                return json_data["data"]["url"]
            else:
                print(f"❌ 图床上传失败: {json_data.get('error', {}).get('message')}")
                return None
    except Exception as e:
        print(f"❌ 图床请求异常: {e}")
        return None

# ================= 5. 推送分发（增加价格和限购表格） =================

def build_markdown_table(products):
    """根据活跃商品列表构建 Markdown 表格，包含价格和限购数"""
    if not products:
        return "暂无商品信息"
    
    # 表头
    table = "| 商品名称 | 价格 | 限购数量 |\n"
    table += "| --- | --- | --- |\n"
    for p in products:
        name = p.get("name", "未知")
        price = p.get("price")
        price_str = str(price) if price is not None and price != "" else "—"
        limit = p.get("buy_limit_num")
        limit_str = str(limit) if limit is not None and limit != "" else "—"
        table += f"| {name} | {price_str} | {limit_str} |\n"
    return table

def push_all(title, body, markdown, image_url):
    """执行双通道推送，markdown 参数现在应包含表格"""
    if NOTIFYME_UUID:
        payload = {
            "data": {
                "uuid": NOTIFYME_UUID, "ttl": 86400, "priority": "high",
                "data": {
                    "title": title, "body": body, "group": "洛克王国", "bigText": True, "record": 1,
                    "markdown": f"{markdown}\n\n![render]({image_url})" if image_url else markdown
                }
            }
        }
        try:
            requests.post(NOTIFYME_SERVER, json=payload, timeout=10)
            print("✅ NotifyMe 推送已发送")
        except Exception as e:
            print(f"❌ NotifyMe 推送失败: {e}")
    
    if BARK_KEY:
        try:
            # Bark 推送不支持 markdown 表格，但可以把表格当作纯文本发送
            # 将 markdown 表格转换为纯文本格式（保留换行）
            plain_table = markdown.replace("|", " ").replace(" --- ", " ").strip()
            full_message = f"{body}\n\n{plain_table}"
            requests.post(f"https://api.day.app/{BARK_KEY}", data={
                "title": title, "body": full_message, "group": "洛克王国", "image": image_url, "isArchive": 1
            }, timeout=10)
            print("✅ Bark 推送已发送")
        except Exception as e:
            print(f"❌ Bark 推送失败: {e}")

# ================= 6. 上报数据到 uniCloud =================

async def send_to_unicloud(status, message, products=None, img_url=None):
    if not UNICLOUD_URL:
        print("ℹ️ 未配置 uniCloud 地址，跳过上报")
        return
    
    try:
        report_data = {
            "task_name": "洛克王国远行商人监控",
            "status": status,
            "message": message,
            "execute_time": get_beijing_time().strftime("%Y-%m-%d %H:%M:%S"),
            "current_products": [p["name"] for p in products] if products else [],
            "product_count": len(products) if products else 0,
            "screenshot_url": img_url,
            "products_detail": [{"name": p["name"], "price": p.get("price"), "limit": p.get("buy_limit_num")} for p in products] if products else []
        }
        response = requests.post(UNICLOUD_URL, json=report_data, timeout=15)
        print(f"✅ 已上报数据到 uniCloud：{response.json()}")
    except Exception as e:
        print(f"❌ uniCloud 上报失败：{str(e)}")

# ================= 7. 主入口 =================

async def main():
    if not IS_MANUAL_TRIGGER:
        if not should_execute():
            print("脚本退出：不在计划执行窗口内（定时任务触发）")
            return
    else:
        print("ℹ️ 手动触发（workflow_dispatch），忽略时间守卫，立即执行完整流程")

    img_url = None
    products = []
    try:
        resp = requests.get(GAME_API_URL, headers={"X-API-Key": ROCOM_API_KEY}, timeout=30)
        resp.raise_for_status()
        json_resp = resp.json()
        raw_data = json_resp.get("data", {})
        err = None if json_resp.get("code") == 0 else json_resp.get("message")
    except Exception as e:
        raw_data, err = None, f"请求异常: {e}"
    
    if err or not raw_data:
        push_all("⚠️ 监控异常", err or "无法获取数据", "无法获取数据", None)
        await send_to_unicloud("失败", err or "无法获取数据")
        return

    processed = process_data_for_template(raw_data)
    products = processed["products"]
    
    # 构建推送正文和Markdown表格
    if products:
        item_names = [p["name"] for p in products]
        push_body = f"当前售卖: {'、'.join(item_names)}"
        markdown_table = build_markdown_table(products)
        markdown_content = f"### 🛒 远行商人已刷新\n\n{markdown_table}"
    else:
        push_body = "当前暂无商品"
        markdown_content = "### 🛒 远行商人已刷新\n\n当前暂无活跃商品"
    
    local_img = await render_to_image(processed)
    img_url = await upload_to_imgbb(local_img)
    
    push_all("📢 远行商人刷新提醒", push_body, markdown_content, img_url)
    await send_to_unicloud("成功", push_body, products, img_url)

if __name__ == "__main__":
    asyncio.run(main())
