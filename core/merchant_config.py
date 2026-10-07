"""多商家配置加载器 — 扫描 merchants/ 目录，建立频道 ↔ 商家映射

启动时自动扫描所有商家目录，加载 merchant.json 和 scrape_targets.yaml，
并建立 Slack 频道 ID → 商家配置的映射关系。

一个 Bot 实例服务所有商家，通过频道 ID 区分。
"""

import json
import logging
from pathlib import Path

import yaml

import config as cfg
from agents.soul_loader import load_merchant_souls

log = logging.getLogger(__name__)

# 所有频道 ID → 商家配置(merchant.json+scrape_targets.yaml)的映射
_channel_map: dict[str, dict] = {}

# 所有商家 ID → 商家配置(merchant.json+scrape_targets.yaml)的映射
_merchant_map: dict[str, dict] = {}


def load_all_merchants() -> None:
    """扫描 merchants/ 目录，加载所有商家配置并建立频道映射

    每个商家目录必须包含:
        merchant.json       — 基本信息（含 slack_channel）
        scrape_targets.yaml — 热词种子关键词
        souls/              — Agent 人格文件
    """
    merchants_dir = cfg.MERCHANTS_DIR

    if not merchants_dir.is_dir():
        raise FileNotFoundError(f"商家目录不存在: {merchants_dir}")

    loaded = 0
    for merchant_dir in sorted(merchants_dir.iterdir()):
        if not merchant_dir.is_dir():
            continue
        
        # merchant_id就是店名，比如harbor_auto_spa
        merchant_id = merchant_dir.name

        # 加载 merchant.json
        config_path = merchant_dir / "merchant.json"
        if not config_path.exists():
            log.warning("跳过 %s — 缺少 merchant.json", merchant_id)
            continue

        # merchant_cfg是merchant.json字典
        with open(config_path, "r", encoding="utf-8") as f:
            merchant_cfg = json.load(f)

        # 加载 scrape_targets.yaml
        targets_path = merchant_dir / "scrape_targets.yaml"
        if targets_path.exists():
            with open(targets_path, "r", encoding="utf-8") as f:
                # merchant_cfg[scrape_target]: {seed_keywords: [keywords]}
                merchant_cfg["scrape_targets"] = yaml.safe_load(f) or {}
        else:
            merchant_cfg["scrape_targets"] = {"seed_keywords": []}
            log.warning("[%s] 缺少 scrape_targets.yaml，热词种子为空", merchant_id)

        # 确保 merchant_id 一致
        merchant_cfg["merchant_id"] = merchant_id
        merchant_cfg["merchant_dir"] = str(merchant_dir)

        # 确保输出目录存在
        output_dir = cfg.OUTPUT_DIR / merchant_id
        output_dir.mkdir(parents=True, exist_ok=True)
        merchant_cfg["output_dir"] = str(output_dir)

        # 加载该商家的所有 Agent 人格
        souls_dir = merchant_dir / "souls"
        try:
            load_merchant_souls(merchant_id, souls_dir)
        except FileNotFoundError as e:
            log.error("跳过 %s — 人格文件不完整: %s", merchant_id, e)
            continue

        # 注册频道映射
        channel_id = merchant_cfg.get("slack_channel", "")
        if not channel_id:
            log.warning("[%s] 未配置 slack_channel，无法接收指令", merchant_id)
        else:
            _channel_map[channel_id] = merchant_cfg

        _merchant_map[merchant_id] = merchant_cfg
        loaded += 1
        log.info("已加载商家: %s → 频道 %s", merchant_cfg.get("store_name", merchant_id), channel_id)

    if loaded == 0:
        raise RuntimeError("没有加载到任何有效的商家配置！请检查 merchants/ 目录")

    log.info("商家加载完成 — 共 %d 个商家, %d 个频道映射", loaded, len(_channel_map))


def get_merchant_by_channel(channel_id: str) -> dict | None:
    """根据 Slack 频道 ID 获取对应的商家配置

    Args:
        channel_id: Slack 频道 ID

    Returns:
        商家配置字典，未找到则返回 None
    """
    return _channel_map.get(channel_id)


def get_merchant(merchant_id: str) -> dict | None:
    """根据商家 ID 获取配置"""
    return _merchant_map.get(merchant_id)


def get_all_merchants() -> dict[str, dict]:
    """获取所有商家配置"""
    return dict(_merchant_map)


def get_seed_keywords(merchant_id: str) -> list[str]:
    """获取商家的热词种子关键词列表"""
    cfg_data = _merchant_map.get(merchant_id, {})
    targets = cfg_data.get("scrape_targets", {})
    return targets.get("seed_keywords", [])
