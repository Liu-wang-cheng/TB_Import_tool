"""配置解析器：将配置文件中的中文名称自动解析为对应的 API ID

在客户端认证完成后调用，支持：
  - 禅道：产品名称 → product_id，项目名称 → project_id
  - Teambition：项目名称 → project_id，场景类型名称 → scenariofieldconfig_id，
                自定义字段名称 → customfield_id，用户中文名 → user_id
"""

import logging
import re
from typing import Optional

from src.source_client import SourceClient
from src.teambition_client import TeambitionClient

logger = logging.getLogger(__name__)

# UUID 格式正则（粗略判断：包含连字符且长度大于20）
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{20,}$")


def _is_uuid(val: str) -> bool:
    return bool(_UUID_RE.match(val))


class ConfigResolver:
    def __init__(self, config: dict, source: SourceClient,
                 teambition: TeambitionClient):
        self.config = config
        self.source = source
        self.teambition = teambition

    def resolve(self):
        """解析所有支持中文名称的配置项。"""
        self._resolve_source()
        self._resolve_teambition()

    # ── 源平台（禅道/Jira） ───────────────────────────

    def _resolve_source(self):
        if self.source.source_type != "zentao":
            return  # Jira 等平台暂不需要解析
        zt = self.config.get("zentao", {})
        filters = zt.setdefault("filters", {})

        # product: 字符串且非纯数字时搜索产品
        product = filters.get("product")
        if product and isinstance(product, str) and not product.isdigit():
            resolved = self.source.search_product(product)
            if resolved:
                filters["product_id"] = resolved
                logger.info("禅道产品 '%s' 解析为 ID: %d", product, resolved)
            else:
                logger.warning("未找到禅道产品: '%s'，请检查名称或改用数字ID", product)
        elif product and isinstance(product, int):
            filters["product_id"] = product
        elif product and isinstance(product, str) and product.isdigit():
            filters["product_id"] = int(product)

        # project: 字符串且非纯数字时搜索项目
        project = filters.get("project")
        if project and isinstance(project, str) and not project.isdigit():
            resolved = self.source.search_project(project)
            if resolved:
                filters["project_id"] = resolved
                logger.info("禅道项目 '%s' 解析为 ID: %d", project, resolved)
            else:
                logger.warning("未找到禅道项目: '%s'，请检查名称或改用数字ID", project)
        elif project and isinstance(project, int):
            filters["project_id"] = project
        elif project and isinstance(project, str) and project.isdigit():
            filters["project_id"] = int(project)

    # ── Teambition ────────────────────────────────────

    def _resolve_teambition(self):
        tb = self.config.setdefault("teambition", {})

        self._resolve_tb_project(tb)
        self._resolve_tb_scenariofieldconfig(tb)
        self._resolve_tb_customfields(tb)
        self._resolve_tb_user_mapping(tb)
        self._resolve_tb_creator(tb)
        self._resolve_tb_project_map(tb)

    def _resolve_tb_project(self, tb: dict):
        """project.name → project_id"""
        project_cfg = tb.get("project", {})
        project_name = project_cfg.get("name", "")
        project_id = project_cfg.get("id", "")

        if project_id:
            self.teambition.project_id = str(project_id)
            tb["project_id"] = str(project_id)
            return

        if project_name:
            resolved = self.teambition.search_project(project_name)
            if resolved:
                self.teambition.project_id = resolved
                tb["project_id"] = resolved
                logger.info("Teambition 项目 '%s' 解析为 ID: %s",
                            project_name, resolved)
            else:
                logger.warning("未找到 Teambition 项目: '%s'，请检查名称或改用ID",
                               project_name)

    def _resolve_tb_scenariofieldconfig(self, tb: dict):
        """scenariofieldconfig_name → scenariofieldconfig_id"""
        project_cfg = tb.get("project", {})
        sfc_name = project_cfg.get("scenariofieldconfig_name", "")
        sfc_id = project_cfg.get("scenariofieldconfig_id", "")

        if sfc_id:
            self.teambition.scenariofieldconfig_id = str(sfc_id)
            tb["scenariofieldconfig_id"] = str(sfc_id)
            return

        if sfc_name:
            resolved = self.teambition.search_scenariofieldconfig(sfc_name)
            if resolved:
                self.teambition.scenariofieldconfig_id = resolved
                tb["scenariofieldconfig_id"] = resolved
                logger.info("场景类型 '%s' 解析为 ID: %s", sfc_name, resolved)
            else:
                logger.warning("未找到场景类型: '%s'，请检查名称或改用ID", sfc_name)

    def _resolve_tb_customfields(self, tb: dict):
        """customfields 中的中文名称 → customfield_ids"""
        cf_cfg = tb.get("customfields", {})
        if not cf_cfg:
            return

        resolved = {}
        for key, val in cf_cfg.items():
            if not val:
                continue
            if isinstance(val, str) and not _is_uuid(val):
                # 可能是中文名称，尝试搜索
                fid = self.teambition.search_customfield(val)
                if fid:
                    resolved[key] = fid
                else:
                    logger.warning("未找到自定义字段 '%s': '%s'，保留原值",
                                   key, val)
                    resolved[key] = val
            else:
                # 已经是 ID 或其他格式
                resolved[key] = val

        tb["customfield_ids"] = resolved
        logger.info("自定义字段解析结果: %s", resolved)

    def _resolve_tb_user_mapping(self, tb: dict):
        """user_mapping 中的中文名称 → user_id"""
        mapping = tb.get("user_mapping", {})
        if not mapping:
            return

        for zt_account, tb_val in list(mapping.items()):
            if not tb_val or not isinstance(tb_val, str):
                continue
            if _is_uuid(tb_val):
                continue  # 已经是 ID
            uid = self.teambition.search_member(tb_val)
            if uid:
                mapping[zt_account] = uid
                logger.info("用户映射 '%s' → '%s' 解析为 ID: %s",
                            zt_account, tb_val, uid)
            else:
                logger.warning("未找到 Teambition 用户 '%s'，保留原值: %s",
                               tb_val, zt_account)

    def _resolve_tb_creator(self, tb: dict):
        """解析 creator_name/creator_id，将最终 UUID 写入 creator_id 并设 operator_id"""
        creator_name = tb.get("creator_name", "").strip()
        creator_id = tb.get("creator_id", "")

        # 主创建人优先：creator_name → 搜索 → UUID
        if creator_name:
            resolved = self.teambition.search_member(creator_name)
            if resolved:
                tb["creator_id"] = resolved
                self.teambition.operator_id = resolved
                logger.info("创建人 '%s' 解析为 ID: %s", creator_name, resolved)
                return

        # 备用创建人：creator_id 可能是中文名或 UUID
        if creator_id:
            if _is_uuid(creator_id):
                self.teambition.operator_id = creator_id
            else:
                resolved = self.teambition.search_member(creator_id)
                if resolved:
                    tb["creator_id"] = resolved
                    self.teambition.operator_id = resolved
                    logger.info("备用创建人 '%s' 解析为 ID: %s", creator_id, resolved)

        if not self.teambition.operator_id:
            logger.warning("未解析到 operator_id，任务将以应用身份创建，"
                           "请检查 creator_name / creator_id 配置")

    def _resolve_tb_project_map(self, tb: dict):
        """project_map 中的项目名称 → project_id"""
        project_map = tb.get("project_map", {})
        if not project_map:
            return

        for module_key, proj_cfg in project_map.items():
            pname = proj_cfg.get("name", "")
            pid = proj_cfg.get("id", "")

            if pid:
                proj_cfg["project_id"] = str(pid)
                continue

            if pname:
                resolved = self.teambition.search_project(pname)
                if resolved:
                    proj_cfg["project_id"] = resolved
                    logger.info("project_map[%s] 项目 '%s' 解析为 ID: %s",
                                module_key, pname, resolved)
                else:
                    logger.warning("project_map[%s] 未找到项目: '%s'",
                                   module_key, pname)
