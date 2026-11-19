import asyncio
import json
import os
import httpx
from config.logger import setup_logging
from plugins_func.register import (
    Action,
    ActionResponse,
    FunctionItem,
    ToolType,
    all_function_registry,
)

TAG = "nmc_weather_patch"
logger = setup_logging()

DATA_DIR = "/opt/xiaozhi-esp32-server/data"
STATIONS_CACHE_PATH = os.path.join(DATA_DIR, "nmc_stations_cache.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.nmc.cn/",
}

PROVINCE_CAPITALS = {
    "湖南": "长沙", "湖北": "武汉", "广东": "广州", "广西": "南宁",
    "浙江": "杭州", "江苏": "南京", "山东": "济南", "河南": "郑州",
    "河北": "石家庄", "山西": "太原", "陕西": "西安", "四川": "成都",
    "云南": "昆明", "贵州": "贵阳", "福建": "福州", "江西": "南昌",
    "安徽": "合肥", "辽宁": "沈阳", "吉林": "长春", "黑龙江": "哈尔滨",
    "海南": "海口", "甘肃": "兰州", "青海": "西宁", "台湾": "台北",
    "内蒙古": "呼和浩特", "新疆": "乌鲁木齐", "西藏": "拉萨", "宁夏": "银川",
}

NMC_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "获取指定城市或所在地的实时天气、气温、湿度、降雨量、风力及未来7天天气预报。"
            "当用户询问今天或未来几天天气、会不会下雨、冷不冷、穿什么衣服时均应调用此工具。"
            "若用户未指明具体城市，location 可省略（自动查询默认城市）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "城市或区县名称，例如长沙、广州、北京、杭州。未指明时可不传。",
                },
                "lang": {
                    "type": "string",
                    "description": "返回语言代码，默认 zh_CN",
                },
            },
            "required": [],
        },
    },
}

_stations_list: list[dict] = []
_stations_lock = asyncio.Lock()


def _is_valid_val(v) -> bool:
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return abs(float(v) - 9999.0) > 1.0 and abs(float(v)) < 9000
    s = str(v).strip()
    return s not in ("", "-", "9999", "9999.0", "null", "None")


async def _ensure_stations_loaded() -> list[dict]:
    global _stations_list
    if _stations_list:
        return _stations_list

    async with _stations_lock:
        if _stations_list:
            return _stations_list
        if os.path.exists(STATIONS_CACHE_PATH):
            try:
                with open(STATIONS_CACHE_PATH, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                    if isinstance(cached, list) and len(cached) > 500:
                        _stations_list = cached
                        return _stations_list
            except Exception:
                pass

        # 从中央气象台一次性拉取全国 34 省级行政区全部站点并本地持久化
        sem = asyncio.Semaphore(8)
        collected: list[dict] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0), headers=HEADERS) as client:
            prov_resp = await client.get("https://www.nmc.cn/rest/province/all")
            provinces = prov_resp.json()

            async def _fetch_prov(pcode: str):
                async with sem:
                    try:
                        r = await client.get(f"https://www.nmc.cn/rest/province/{pcode}")
                        if r.status_code == 200:
                            return r.json()
                    except Exception:
                        pass
                return []

            results = await asyncio.gather(*[_fetch_prov(p["code"]) for p in provinces if p.get("code")])
            for arr in results:
                if isinstance(arr, list):
                    for item in arr:
                        if item.get("code") and item.get("city"):
                            collected.append(
                                {
                                    "code": item["code"],
                                    "province": item.get("province", ""),
                                    "city": item["city"],
                                }
                            )

        if collected:
            _stations_list = collected
            try:
                with open(STATIONS_CACHE_PATH, "w", encoding="utf-8") as f:
                    json.dump(collected, f, ensure_ascii=False)
            except Exception:
                pass
        return _stations_list


def _match_station(query: str, stations: list[dict]) -> dict | None:
    q = (query or "").strip()
    if not q:
        return None
    for suffix in ("省", "市", "自治区", "特别行政区"):
        if q.endswith(suffix) and len(q) > len(suffix) + 1:
            q_clean = q[: -len(suffix)]
            break
    else:
        q_clean = q

    if q_clean in PROVINCE_CAPITALS:
        q = PROVINCE_CAPITALS[q_clean]
        q_clean = q

    # 1. 精确匹配 city == q 或 city == q_clean 或 city == q_clean + "市"
    for st in stations:
        c = st["city"]
        if c == q or c == q_clean or c == f"{q_clean}市":
            return st

    # 2. 去掉区/县后缀再匹配
    q_short = q_clean
    for suf in ("区", "县", "州", "旗"):
        if q_short.endswith(suf) and len(q_short) >= 3:
            q_short = q_short[:-1]
            break

    for st in stations:
        c = st["city"]
        c_base = c.rstrip("市县区")
        if c_base == q_short or q_clean in c or c in q:
            return st
    return None


async def _fetch_overseas_wttr(location: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0), headers=HEADERS) as client:
            r = await client.get(f"https://wttr.in/{location}", params={"format": "j1", "lang": "zh"})
            if r.status_code != 200:
                return None
            data = r.json()
            cur = (data.get("current_condition") or [{}])[0]
            desc_list = cur.get("lang_zh") or cur.get("weatherDesc") or [{}]
            desc = desc_list[0].get("value", "未知") if desc_list else "未知"
            temp = cur.get("temp_C", "?")
            feels = cur.get("FeelsLikeC", "?")
            hum = cur.get("humidity", "?")
            lines = [f"查询位置：{location}", f"当前天气：{desc}，气温 {temp}℃（体感 {feels}℃），湿度 {hum}%"]
            for d in (data.get("weather") or [])[:5]:
                dt = d.get("date", "")
                tmax = d.get("maxtempC", "?")
                tmin = d.get("mintempC", "?")
                hourly = d.get("hourly") or []
                d_desc = ""
                if len(hourly) >= 4:
                    dl = hourly[4].get("lang_zh") or hourly[4].get("weatherDesc") or [{}]
                    d_desc = dl[0].get("value", "") if dl else ""
                lines.append(f"{dt}：{d_desc}，{tmin}~{tmax}℃")
            return "\n".join(lines)
    except Exception:
        return None


async def nmc_get_weather(conn, location: str = None, lang: str = "zh_CN"):
    from core.utils.cache.manager import CacheType, cache_manager

    weather_cfg = conn.config.get("plugins", {}).get("get_weather", {})
    default_location = weather_cfg.get("default_location", "长沙")
    target_loc = (location or "").strip() or default_location

    cache_key = f"nmc_weather_{target_loc}_{lang}"
    cached = cache_manager.get(CacheType.WEATHER, cache_key)
    if cached:
        return ActionResponse(Action.REQLLM, cached, None)

    try:
        stations = await _ensure_stations_loaded()
        st = _match_station(target_loc, stations)
        if not st:
            overseas = await _fetch_overseas_wttr(target_loc)
            if overseas:
                cache_manager.set(CacheType.WEATHER, cache_key, overseas)
                return ActionResponse(Action.REQLLM, overseas, None)
            return ActionResponse(Action.REQLLM, f"未找到「{target_loc}」对应的气象站点，请确认城市名称。", None)

        async with httpx.AsyncClient(timeout=httpx.Timeout(7.0, connect=3.5), headers=HEADERS) as client:
            resp = await client.get("https://www.nmc.cn/rest/weather", params={"stationid": st["code"]})
            payload = resp.json()

        if resp.status_code != 200 or payload.get("code") != 0:
            return ActionResponse(Action.REQLLM, f"气象台服务暂时繁忙，未能获取 {target_loc} 天气。", None)

        data = payload.get("data") or {}
        real = data.get("real") or {}
        rw = real.get("weather") or {}
        wind = real.get("wind") or {}
        warn = real.get("warn") or {}
        predict_list = (data.get("predict") or {}).get("detail") or []

        city_label = f"{st.get('province', '')}{st.get('city', target_loc)}"
        cur_info = rw.get("info") if _is_valid_val(rw.get("info")) else ""
        if not cur_info and predict_list:
            today = predict_list[0]
            d_info = ((today.get("day") or {}).get("weather") or {}).get("info", "")
            n_info = ((today.get("night") or {}).get("weather") or {}).get("info", "")
            cur_info = d_info if _is_valid_val(d_info) else (n_info if _is_valid_val(n_info) else "多云")

        parts = [f"查询地点：{city_label}（中央气象台实况 {real.get('publish_time', '')}）"]
        rt_items = []
        if cur_info:
            rt_items.append(f"天气 {cur_info}")
        if _is_valid_val(rw.get("temperature")):
            rt_items.append(f"实时气温 {rw['temperature']}℃")
        if _is_valid_val(rw.get("feelst")):
            rt_items.append(f"体感 {rw['feelst']}℃")
        if _is_valid_val(rw.get("humidity")):
            rt_items.append(f"相对湿度 {int(float(rw['humidity']))}%")
        if _is_valid_val(rw.get("rain")):
            rain_mm = float(rw["rain"])
            rt_items.append(f"近1小时降水量 {rain_mm}mm" if rain_mm > 0 else "当前无降雨(0mm)")
        w_dir = wind.get("direct") if _is_valid_val(wind.get("direct")) else ""
        w_pow = wind.get("power") if _is_valid_val(wind.get("power")) else ""
        if w_dir or w_pow:
            rt_items.append(f"风力 {w_dir}{w_pow}")
        if rt_items:
            parts.append("当前实况：" + "，".join(rt_items))

        if _is_valid_val(warn.get("alert")):
            parts.append(f"气象预警：{warn.get('alert')}")

        if predict_list:
            parts.append("\n未来7天天气预报：")
            for item in predict_list[:7]:
                dt = item.get("date", "")
                dw = ((item.get("day") or {}).get("weather") or {})
                nw = ((item.get("night") or {}).get("weather") or {})
                d_txt = dw.get("info") if _is_valid_val(dw.get("info")) else ""
                n_txt = nw.get("info") if _is_valid_val(nw.get("info")) else ""
                t_high = dw.get("temperature") if _is_valid_val(dw.get("temperature")) else ""
                t_low = nw.get("temperature") if _is_valid_val(nw.get("temperature")) else ""
                cond = d_txt if (not n_txt or d_txt == n_txt) else (f"{d_txt}转{n_txt}" if d_txt else n_txt)
                temp_range = f"{t_low}~{t_high}℃" if (t_low and t_high) else f"{t_high or t_low}℃"
                parts.append(f"- {dt}：{cond}，气温 {temp_range}")

        report = "\n".join(parts)
        cache_manager.set(CacheType.WEATHER, cache_key, report)
        logger.bind(tag=TAG).info(f"中央气象台查询成功: {city_label}")
        return ActionResponse(Action.REQLLM, report, None)
    except Exception as exc:
        logger.bind(tag=TAG).error(f"中央气象台查询异常: {exc}")
        return ActionResponse(Action.REQLLM, f"查询 {target_loc} 天气时网络异常，请稍后再试。", None)


def apply_nmc_weather_patch():
    # 先导入原始模块使其进入 sys.modules，防止后续 auto_import_modules 覆盖我们的注册项
    import plugins_func.functions.get_weather as orig_mod

    orig_mod.GET_WEATHER_FUNCTION_DESC = NMC_WEATHER_FUNCTION_DESC
    orig_mod.get_weather = nmc_get_weather
    all_function_registry["get_weather"] = FunctionItem(
        "get_weather",
        NMC_WEATHER_FUNCTION_DESC,
        nmc_get_weather,
        ToolType.SYSTEM_CTL,
    )
    print("[NMCWeather] 已启用中央气象台免Key全国实时+7天天气插件", flush=True)
