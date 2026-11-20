import asyncio
import json
import logging
import os
import time
import weakref
import httpx
import patch_registry
import xz_common

TAG = "nmc_weather_patch"


class _LazyLoggerProxy:
    """Lazy wrapper avoiding upstream `setup_logging()` side effects at import time (H-1)."""

    def __init__(self) -> None:
        self._real = None

    def _get(self):
        if self._real is None:
            try:
                from config.logger import setup_logging

                self._real = setup_logging()
            except Exception:
                self._real = logging.getLogger(TAG)
        return self._real

    def bind(self, **kwargs):
        real = self._get()
        if hasattr(real, "bind"):
            return real.bind(**kwargs)
        return real

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


logger = _LazyLoggerProxy()

DATA_DIR = xz_common.STATE_DIR
STATIONS_CACHE_PATH = os.path.join(xz_common.STATE_DIR, "nmc_stations_cache.json")
_SEED_STATIONS_PATHS = (
    os.path.join(xz_common.ROOT_DIR, "config_templates", "nmc_stations_cache.json"),
    os.path.join(xz_common.DATA_DIR, "nmc_stations_cache.json"),
)

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
_stations_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()
_async_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = weakref.WeakKeyDictionary()
_MIN_COMPLETE_STATIONS = 500
_MIN_PROVINCE_SUCCESS_RATIO = 0.90
_MIN_EXPECTED_PROVINCES = 25

# C-7: Exponential backoff on station-table fetch failure (5 min -> 10m -> 20m -> 40m -> 1h max)
_BACKOFF_BASE_SECONDS = 300.0
_BACKOFF_MAX_SECONDS = 3600.0
_fetch_fail_count: int = 0
_next_fetch_retry_at: float = 0.0


def _get_stations_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lk = _stations_locks.get(loop)
    if lk is None:
        lk = asyncio.Lock()
        _stations_locks[loop] = lk
    return lk


def _get_async_client() -> httpx.AsyncClient:
    """Reuse a module-level httpx.AsyncClient per running event loop (C-7)."""
    loop = asyncio.get_running_loop()
    client = _async_clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=4.0),
            headers=HEADERS,
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
        )
        _async_clients[loop] = client
    return client


async def close_async_client() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    client = _async_clients.pop(loop, None)
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception:
            pass


def reset_station_fetch_state() -> None:
    """Reset in-memory station list and failure backoff (primarily for isolated unit tests)."""
    global _stations_list, _fetch_fail_count, _next_fetch_retry_at
    _stations_list = []
    _fetch_fail_count = 0
    _next_fetch_retry_at = 0.0


def get_station_fetch_backoff_state() -> dict:
    now = time.monotonic()
    remaining = max(0.0, _next_fetch_retry_at - now)
    return {
        "fail_count": _fetch_fail_count,
        "next_retry_at": _next_fetch_retry_at,
        "backoff_remaining_seconds": round(remaining, 2),
        "cached_stations": len(_stations_list),
    }


def _record_fetch_failure() -> float:
    global _fetch_fail_count, _next_fetch_retry_at
    _fetch_fail_count += 1
    delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** min(_fetch_fail_count - 1, 6)))
    _next_fetch_retry_at = time.monotonic() + delay
    return delay


def _reset_fetch_backoff() -> None:
    global _fetch_fail_count, _next_fetch_retry_at
    _fetch_fail_count = 0
    _next_fetch_retry_at = 0.0


def _filter_valid_stations(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict) and item.get("code") and item.get("city"):
            out.append(item)
    return out


def _atomic_write_stations_cache(path: str, stations: list[dict]) -> None:
    xz_common.atomic_write_json(path, stations, indent=None)


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

    async with _get_stations_lock():
        if _stations_list:
            return _stations_list
        existing_disk: list[dict] = []
        cache_candidates = [STATIONS_CACHE_PATH]
        if STATIONS_CACHE_PATH == os.path.join(xz_common.STATE_DIR, "nmc_stations_cache.json"):
            cache_candidates.extend(_SEED_STATIONS_PATHS)
        for cand_path in cache_candidates:
            if os.path.exists(cand_path):
                try:
                    with open(cand_path, "r", encoding="utf-8") as f:
                        cached = _filter_valid_stations(json.load(f))
                        if cached:
                            if len(cached) > len(existing_disk):
                                existing_disk = cached
                            # C-7: Once nmc_stations_cache.json has valid complete data, ALWAYS prefer local cache
                            if len(cached) > _MIN_COMPLETE_STATIONS:
                                _stations_list = cached
                                _reset_fetch_backoff()
                                return _stations_list
                except Exception:
                    pass

        # C-7: Respect exponential backoff (5 min .. 1 hour) before hitting nmc.cn again
        now = time.monotonic()
        if _next_fetch_retry_at > 0.0 and now < _next_fetch_retry_at:
            return existing_disk

        # 从中央气象台一次性拉取全国 34 省级行政区全部站点并本地持久化
        sem = asyncio.Semaphore(8)
        collected: list[dict] = []
        total_provinces = 0
        ok_provinces = 0
        try:
            client = _get_async_client()
            prov_resp = await client.get(
                "https://www.nmc.cn/rest/province/all",
                timeout=httpx.Timeout(8.0, connect=4.0),
            )
            if prov_resp.status_code != 200:
                raise RuntimeError(f"province/all HTTP {prov_resp.status_code}")
            provinces = prov_resp.json()
            if not isinstance(provinces, list):
                provinces = []
            prov_codes = [p["code"] for p in provinces if isinstance(p, dict) and p.get("code")]
            total_provinces = len(prov_codes)

            async def _fetch_prov(pcode: str) -> tuple[bool, list]:
                async with sem:
                    try:
                        r = await client.get(
                            f"https://www.nmc.cn/rest/province/{pcode}",
                            timeout=httpx.Timeout(8.0, connect=4.0),
                        )
                        if r.status_code == 200:
                            data = r.json()
                            if isinstance(data, list):
                                return True, data
                    except Exception:
                        pass
                return False, []

            results = await asyncio.gather(*[_fetch_prov(code) for code in prov_codes])
            for ok_flag, arr in results:
                if ok_flag:
                    ok_provinces += 1
                for item in arr:
                    if isinstance(item, dict) and item.get("code") and item.get("city"):
                        collected.append(
                            {
                                "code": item["code"],
                                "province": item.get("province", ""),
                                "city": item["city"],
                            }
                        )
        except Exception as exc:
            delay = _record_fetch_failure()
            logger.bind(tag=TAG).warning(
                f"拉取中央气象台站点列表失败: {exc}，将在 {delay:.0f}s 后重试 (第 {_fetch_fail_count} 次失败)"
            )
            return existing_disk

        is_complete = (
            total_provinces >= _MIN_EXPECTED_PROVINCES
            and (ok_provinces / float(total_provinces)) >= _MIN_PROVINCE_SUCCESS_RATIO
            and len(collected) > _MIN_COMPLETE_STATIONS
        )
        if is_complete:
            _stations_list = collected
            _reset_fetch_backoff()
            if len(collected) >= len(existing_disk):
                try:
                    _atomic_write_stations_cache(STATIONS_CACHE_PATH, collected)
                except Exception as exc:
                    logger.bind(tag=TAG).warning(f"写入气象站点缓存失败: {exc}")
            return _stations_list

        # 部分省份抓取失败时绝不将残缺站点表写盘覆盖完整缓存，也不永久锁死内存表，按指数退避稍后重试
        delay = _record_fetch_failure()
        logger.bind(tag=TAG).warning(
            f"中央气象台站点表拉取不完整 ({ok_provinces}/{total_provinces} 省, {len(collected)} 站点)，"
            f"跳过落盘以免覆盖完整缓存，{delay:.0f}s 后重试"
        )
        return collected if len(collected) >= len(existing_disk) else existing_disk


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
        client = _get_async_client()
        r = await client.get(
            f"https://wttr.in/{location}",
            params={"format": "j1", "lang": "zh"},
            timeout=httpx.Timeout(6.0, connect=3.0),
        )
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


async def nmc_get_weather(conn, location: str = None, lang: str = "zh_CN", *args, **kwargs):
    from core.utils.cache.manager import CacheType, cache_manager
    from plugins_func.register import Action, ActionResponse

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

        client = _get_async_client()
        resp = await client.get(
            "https://www.nmc.cn/rest/weather",
            params={"stationid": st["code"]},
            timeout=httpx.Timeout(7.0, connect=3.5),
        )
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
    from plugins_func.register import FunctionItem, ToolType, all_function_registry

    orig_fn = getattr(orig_mod, "get_weather", None)
    ok_sig, det_sig = patch_registry.verify_signature_contract(
        orig_fn, required_params=("conn",), require_async=None
    )
    if not ok_sig:
        patch_registry.record_patch(
            "nmc_weather",
            "skipped_contract",
            target="plugins_func.functions.get_weather",
            detail=det_sig,
            contract_ok=False,
            category="experience",
        )
        print(f"[NMCWeather] 跳过挂载天气补丁（签名契约不匹配）: {det_sig}", flush=True)
        return

    orig_mod.GET_WEATHER_FUNCTION_DESC = NMC_WEATHER_FUNCTION_DESC
    orig_mod.get_weather = nmc_get_weather
    rebound = patch_registry.rebind(orig_fn, nmc_get_weather) if orig_fn is not None else []
    all_function_registry["get_weather"] = FunctionItem(
        "get_weather",
        NMC_WEATHER_FUNCTION_DESC,
        nmc_get_weather,
        ToolType.SYSTEM_CTL,
    )
    patch_registry.record_patch(
        "nmc_weather",
        "ok",
        target="plugins_func.functions.get_weather",
        detail=det_sig,
        contract_ok=True,
        category="experience",
        rebound=rebound,
    )
    print("[NMCWeather] 已启用中央气象台免Key全国实时+7天天气插件", flush=True)
