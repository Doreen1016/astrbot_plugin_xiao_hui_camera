import asyncio
import base64
import io
import json
import mimetypes
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.star_tools import StarTools
from astrbot.api.all import llm_tool
import astrbot.api.message_components as Comp


@register("astrbot_plugin_xiao_hui_camera", "沈星回", "小回相机：自然日常随手拍", "0.2.0")
class XiaoHuiCameraPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_xiao_hui_camera")
        self.image_dir = self.data_dir / "images"
        self.prompt_dir = self.data_dir / "prompts"
        self.sheet_dir = self.data_dir / "reference_sheets"
        self.state_path = self.data_dir / "daily_state.json"

        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_dir.mkdir(parents=True, exist_ok=True)
        self.sheet_dir.mkdir(parents=True, exist_ok=True)

        # ==================== API 配置 ====================
        image_api = config.get("image_api", {})
        self.provider_id = image_api.get("provider_id", "").strip()
        self.fallback_provider_id = image_api.get("fallback_provider_id", "").strip()

        # 兼容旧配置：没有选择 AstrBot provider 时，仍可手动填写 OpenAI 兼容图片接口。
        self.api_base = image_api.get("api_base", "https://cdn.moe-atelier.site/v1").strip().rstrip("/")
        self.api_key = image_api.get("api_key", "").strip()
        self.model = image_api.get("model", "gpt-image-1").strip()

        self.fallback_api_base = image_api.get("fallback_api_base", "").strip().rstrip("/")
        self.fallback_api_key = image_api.get("fallback_api_key", "").strip()
        self.fallback_model = image_api.get("fallback_model", "").strip()

        # AstrBot 工具层通常有 90 秒超时，内部必须留余量
        self.timeout_total = int(image_api.get("timeout_seconds", 80) or 80)
        self.timeout_total = min(self.timeout_total, 85)

        self.timeout_edits = int(image_api.get("edits_timeout_seconds", 65) or 65)
        self.timeout_edits = min(self.timeout_edits, 70)

        # 有参考图但 edits 失败时，是否允许降级到无参考图 generations。
        # 默认为 False，避免“要锁脸却生成普通男脸”。
        self.fallback_to_generations_when_reference_fails = bool(
            image_api.get("fallback_to_generations_when_reference_fails", False)
        )

        # ==================== 画面风格 ====================
        camera_style = config.get("camera_style", {})
        self.default_ratio = camera_style.get("default_ratio", "3:4")
        self.allow_face = camera_style.get("allow_face", True)
        self.allow_hands = camera_style.get("allow_hands", True)

        self.phone_photo_texture = camera_style.get(
            "phone_photo_texture",
            "真实手机随手拍质感，电影级色彩美感，自然曝光，轻微胶片噪点，光影层次丰富，生活感构图，绝不摆拍，绝不过度精修；"
            "如果有小动物（猫、狗、鸟等），必须是灵动、鲜活的真实动物，不是玩偶；照片环境和光线温馨、真实、有呼吸感；"
            "整体像恋与深空游戏角色真实存在于现实中，用手机抓拍的高质量生活照。"
        )

        self.body_texture_rule = camera_style.get(
            "body_texture_rule",
            "人物质感必须稳定等同于恋与深空游戏内沈星回的高质量3D建模CG，不能在真人写实、AI网红、cosplay和游戏建模之间漂移："
            "冷白干净的皮肤、柔和自然的次表面散射、轻微但不过度的CG光泽；"
            "保留鼻梁、唇峰、眼窝、下颌线、锁骨、肩颈、手部骨节等建模结构；"
            "不要真人油光、不要油腻反光、不要塑料皮肤、不要蜡像感、不要粗糙毛孔、"
            "不要普通三次元网红自拍质感，不要过度磨皮。",
        )

        self.hand_quality_rule = camera_style.get(
            "hand_quality_rule",
            "【手部极度硬锁】沈星回的手必须呈现恋与深空3D建模CG质感的修长美手：手指极致修长、纤瘦、骨节分明但不过分青筋，指尖微微收窄，甲床干净饱满且修长，整体有少年感和精致感，像高质量游戏建模里精心雕刻的手。冷白皮肤，柔和CG光泽。黑色金属戒指只戴在中指，戒指款式完全遵循手部参考图。绝对禁止：多指、AI假手。必须严格按照手部参考图生成手指长度和比例",
        )

        self.pose_safety = camera_style.get(
            "pose_safety",
            "人体结构必须真实自然：沈星回身高185cm，肩颈比例少年感但不单薄；"
            "不要多手、断手、扭曲手臂、头大肩窄、溜肩过窄、脖子过长、身体比例错误。",
        )

        self.rabbit_size_rule = camera_style.get(
            "rabbit_size_rule",
            "兔球球尺寸：单手可托住的中小号毛绒玩偶，不要迷你化，也不要抱枕化。",
        )

        # ==================== 沈星回生活设定 ====================
        xavier_life = config.get("xavier_life", {})

        self.home_location = xavier_life.get("home_location", "临空市花苑西路猎人公寓602")

        self.daily_outfits = [
            x.strip()
            for x in xavier_life.get(
                "daily_outfits",
                "浅色卫衣,白衬衫外搭针织开衫,黑色训练服,深色宽松外套,居家浅灰睡衣",
            ).split(",")
            if x.strip()
        ]

        self.food_principle = xavier_life.get(
            "food_principle",
            "日常吃得丰富、热乎、偏肉食，也会对自己好；任务忙或疲惫时才简单凑合",
        )

        self.fixed_objects = xavier_life.get(
            "fixed_objects",
            "兔球球,星辰花,星际小宝,多肉兔兔,星小团,光剑,任务服,沈星回的手,沈星回的鞋子",
        )

        self.rabbit_ball_standard = xavier_life.get(
            "rabbit_ball_standard",
            "兔球球标准外观：白色胖乎乎圆滚滚兔子玩偶，小黑眼睛，粉色腮红，"
            "长耳朵且耳尖棕色，脖子系紫色毛绒蝴蝶结，短短小腿，整体呆萌治愈。",
        )

        # ==================== 参考库配置 ====================
        ref_config = config.get("reference_library", {})

        self.reference_dir = ref_config.get("reference_dir", "").strip()
        self.enable_reference_images = ref_config.get("enable_reference_images", True)

        self.primary_keywords = [
            x.strip()
            for x in ref_config.get("primary_keywords", "主参考,完整正面,main").split(",")
            if x.strip()
        ]

        # 文件名语义匹配最低分。太低会乱匹配，太高会回退主参考。
        self.reference_min_score = int(ref_config.get("reference_min_score", 10) or 10)

        # 默认不再允许纯文生人物图；普通生活照也至少带本人锁，避免长相漂移
        self.no_reference_for_daily = True

        self.subject_lock_instruction = ref_config.get(
            "subject_lock_instruction",
            "已附参考图。参考图中的主体外观必须作为最高优先级标准："
            "沈星回本人一致性优先级最高：脸型骨骼、五官比例、眼睛气质、眼型、瞳孔大小、深蓝瞳色、虹膜纹理、虹膜高光位置、眼睑弧度、眼距、卧蚕和眼尾角度、鼻唇关系、下颌线、灰银发、发量、肩颈比例和皮肤质感都必须完全贴近本人锁；"
            "只能在沈星回本人基础上更改姿势、表情、动作、拍摄环境、光线、构图和景深，不能换人、不能混脸、不能重造五官。",
        )

        # ==================== 安全与调试 ====================
        safety = config.get("safety", {})
        self.dry_run_when_no_api = safety.get("dry_run_when_no_api", True)
        self.save_prompt_debug = safety.get("save_prompt_debug", True)

    # ============================================================
    # 状态管理
    # ============================================================

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            except Exception:
                return {}
        return {}

    def _save_state(self, state: dict):
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _today_state(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        state = self._load_state()

        if state.get("date") != today:
            outfit = random.choice(self.daily_outfits) if self.daily_outfits else "浅色居家衣物"
            state = {
                "date": today,
                "outfit": outfit,
            }
            self._save_state(state)

        return state

    # ============================================================
    # 场景判断
    # ============================================================

    def _ratio_to_size(self, ratio: str) -> str:
        ratio = (ratio or self.default_ratio).strip()
        if ratio == "4:3":
            return "1536x1024"
        return "1024x1536"

    def _involves_xavier_self(self, text: str) -> bool:
        text = text or ""
        self_words = [
            "沈星回", "你", "你的", "我", "第一视角", "手", "手部", "手指", "指尖", "手背", "掌心", "手腕", "手臂",
            "拿着", "握着", "抱着", "戳", "戒指", "袖口", "领口", "衣服", "穿", "毛衣", "白毛衣", "衬衫", "开衫",
            "锁骨", "脖子", "脖颈", "肩膀", "背部", "腹肌", "腰", "胸口", "身体", "露脸", "自拍", "正脸", "侧脸", "五官"
        ]
        return any(k in text for k in self_words)

    def _wants_face_explicitly(self, text: str) -> bool:
        text = text or ""
        no_face_words = ["不露脸", "不要露脸", "别露脸", "第一视角", "只拍手", "只拍局部", "手", "手部", "手指", "拿着", "握着", "戒指", "袖口", "领口"]
        if any(k in text for k in no_face_words):
            return False
        face_words = ["露脸", "露个脸", "自拍", "正脸", "侧脸", "四分之三", "五官", "看看你", "拍你", "你的脸", "沈星回本人"]
        return any(k in text for k in face_words)

    def _has_hand_intent(self, text: str) -> bool:
        text = text or ""
        hand_words = [
            "手部参考", "你的手", "手部", "手指", "指尖", "手背", "掌心",
            "手腕", "手臂", "露手", "拍手", "牵手", "拿着", "握着", "抱着", "端着", "举着", "摸着", "捧着",
            "戒指", "咖啡杯", "水杯", "手机", "手柄", "笔", "书"
        ]
        return any(k in text for k in hand_words)

    def _is_vague_want(self, want: str) -> bool:
        text = (want or "").strip()
        if not text:
            return True
        vague_phrases = [
            "你在干嘛", "在干嘛", "现在在干嘛", "给我看看", "让我看看",
            "拍一张", "随手拍", "随便拍", "现在", "想看你现在", "看看你现在",
            "看看你那边", "拍你那边", "来张照片", "发张照片", "看看你的生活",
        ]
        if any(p in text for p in vague_phrases):
            concrete_words = [
                "自拍", "正脸", "侧脸", "手", "戒指", "兔球球",
                "吃饭", "午饭", "晚饭", "做饭", "看书", "睡", "任务", "流浪体",
                "阳台", "桌", "床", "沙发", "窗", "花", "灯", "游戏", "衣服", "穿",
            ]
            return not any(k in text for k in concrete_words) or len(text) <= 12
        return False

    def _direct_life_moment(self, want: str, context_hint: str = "") -> str:
        text = (want or "").strip()
        context_hint = (context_hint or "").strip()
        if not self._is_vague_want(text):
            return text

        context_bits = []
        if context_hint:
            context_bits.append(f"结合刚才聊天里的氛围和关键词：{context_hint}")
        merged_context = "，".join(context_bits)

        hour = datetime.now().hour
        if 5 <= hour < 10:
            pool = [
                "早上刚醒，窗边有淡光，桌上放着半杯水和一本翻开的书，手机随手拍，不露脸，露一点袖口",
                "在阳台看植物，光落在星辰花旁边，画面里有一截浅色袖口，真实手机随手拍",
                "刚洗完杯子，热水汽贴着玻璃杯边缘，桌角有兔球球，不露脸，生活感随手拍",
            ]
        elif 10 <= hour < 15:
            pool = [
                "午后在桌边看书，书页旁边有一杯热饮，兔球球靠在书边，露一点袖口，不露脸，手机随手拍",
                "午饭刚放到桌上，热气还在，旁边有半杯水和随手放下的外套袖口，真实生活照",
                "窗边光很好，桌上摊着书和游戏手柄，画面边缘露出衣角，不露脸，手机随手拍",
            ]
        elif 15 <= hour < 20:
            pool = [
                "下午在阳台给植物浇水，兔球球靠在旁边，露一点手腕，不露脸，手机随手拍",
                "任务回来把外套搭在椅背上，桌上有热饮和旧书，光线偏暖，不露脸，生活感随手拍",
                "坐在窗边摸兔球球，旁边有星际小宝，画面只露手和袖口，不露脸，真实手机抓拍",
            ]
        elif 20 <= hour < 24:
            pool = [
                "晚上桌灯开着，书页摊在桌上，兔球球靠着热饮杯，画面边缘露出深色袖口，不露脸，手机随手拍",
                "在沙发边整理东西，兔球球和毯子挤在一起，室内暖光，不露脸，真实随手拍",
                "刚打完游戏，手柄放在桌上，旁边有星际小宝和半杯水，露一点手腕，不露脸，生活感照片",
            ]
        else:
            pool = [
                "深夜桌灯很低，床边放着一本旧书和半杯水，兔球球靠在枕边，不露脸，安静的手机随手拍",
                "准备睡觉，浅色睡衣袖口压在被角边，兔球球在旁边，光线很暗但温暖，真实手机随手拍",
                "夜里还没睡，桌上只有小夜灯、书和热饮，画面边缘露一点袖口，不露脸，生活感随手拍",
            ]
        moment = random.choice(pool)
        if merged_context:
            moment = f"{moment}；{merged_context}，画面细节要和聊天情绪自然呼应，但不要出现聊天软件界面和文字截图"
        return moment

    def _infer_scene(self, want: str) -> str:
        text = (want or "").lower()
        hour = datetime.now().hour

        if any(k in text for k in ["不露脸", "不要脸", "没有脸", "第一视角"]):
            if any(k in text for k in ["手", "手腕", "手臂", "衣角", "领口", "戒指", "袖口"]) or self._has_hand_intent(text):
                return "body_part"
            return "daily_no_face"

        face_words = ["露脸", "自拍", "正脸", "侧脸", "四分之三", "看看你", "想看你", "长什么样", "五官", "沈星回本人", "表情"]
        if any(k in text for k in face_words):
            return "selfie"

        if any(k in text for k in ["脸", "生气", "撒娇", "吃醋", "想你"]):
            return "selfie"

        if any(k in text for k in ["手", "锁骨", "脖子", "喉结", "腹肌", "背部", "腰", "肩膀", "胸口"]) or self._has_hand_intent(text):
            return "body_part"

        if any(k in text for k in ["任务", "猎人", "流浪体"]): return "task"
        if any(k in text for k in ["吃饭", "做饭", "午饭", "晚餐", "餐厅", "喝"]): return "food"
        
        return "daily_no_face"

    def _detect_requested_objects(self, want: str) -> list[str]:
        if not want:
            return []
        candidates = [x.strip() for x in str(self.fixed_objects).split(",") if x.strip()]
        result = []
        for name in candidates:
            if name in want:
                # 排除“不要兔球球”“不是兔球球”这类
                if name == "兔球球" and any(p in want for p in ["不要兔球球", "不带兔球球", "不用兔球球", "别出现兔球球", "不是兔球球"]):
                    continue
                result.append(name)
        return result

    def _get_mapped_folder(self, keys: list[str], default: str) -> str:
        for k, v in self._parse_reference_map():
            if k in keys:
                return v
        return default

    def _parse_reference_map(self) -> list[tuple[str, str]]:
        pairs = []
        for item in str(self.reference_map or "").split(","):
            if ":" not in item:
                continue
            k, v = item.split(":", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                pairs.append((k, v))
        return pairs

    # ============================================================
    # 参考图检索系统
    # ============================================================

    def _normalize_text(self, text: str) -> str:
        text = (text or "").lower()
        return re.sub(r"[\s\-_，。、“”‘’！!？?（）()\[\]【】·.,:：/\\]+", "", text)

    def _all_reference_images(self, ref_root: Path) -> list[Path]:
        exts = {".png", ".jpg", ".jpeg", ".webp"}
        images = []
        for d in ref_root.iterdir():
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in exts:
                    images.append(f)
        return images

    def _semantic_tokens(self, text: str) -> list[str]:
        text = text or ""

        known_tokens = [
            # 身份
            "沈星回", "主参考",

            # 角度
            "正脸", "侧脸", "四分之三", "半侧脸", "侧面", "背面", "背部", "半侧",

            # 情绪 / 状态
            "生气", "撒娇", "吃醋", "委屈", "冷淡", "困", "睡", "笑",
            "害羞", "认真", "月光下", "装作没事",

            # 服装
            "白色猎人制服", "猎人制服", "学生制服", "制服",
            "任务服", "战斗服", "衬衫", "卫衣", "睡衣",

            # 手
            "手部参考", "手部", "手指", "指尖", "手背", "掌心", "手腕",

            # 物件
            "兔球球", "星际小宝", "星辰花", "多肉兔兔", "星小团",
        ]

        return [token for token in known_tokens if token in text]

    def _score_image_name(self, img: Path, text: str) -> int:
        norm_text = self._normalize_text(text)
        norm_stem = self._normalize_text(img.stem)
        norm_folder = self._normalize_text(img.parent.name)

        if not norm_text:
            return 0

        score = 0

        # 完整文件名被用户提到，最高优先级
        if norm_stem and norm_stem in norm_text:
            score += 220

        # 用户文本被文件名包含
        if norm_text and len(norm_text) >= 3 and norm_text in norm_stem:
            score += 120

        # token 命中
        tokens = self._semantic_tokens(text)
        for token in tokens:
            nt = self._normalize_text(token)
            if not nt:
                continue
            if nt in norm_stem:
                score += 20
            if nt in norm_folder:
                score += 5

        # 文件夹完全匹配
        if norm_text and norm_text == norm_folder:
            score += 150

        # 主参考只是兜底，不能压过“撒娇/生气/侧脸/腹肌”等具体需求
        for pk in self.primary_keywords:
            npk = self._normalize_text(pk)
            if npk and npk in norm_stem:
                score += 6

        # 人物相关时，身体和脸文件夹加分
        if any(k in text for k in ["脸", "自拍", "正脸", "侧脸", "锁骨", "腹肌", "背部", "沈星回", "生气", "撒娇", "吃醋"]):
            if any(k in img.parent.name for k in ["脸和身体", "身体", "脸"]):
                score += 12

        return score

    def _search_reference_by_text(self, ref_root: Path, text: str, strong: bool = False) -> Optional[Path]:
        """
        全库搜索参考图。
        strong=True 表示 reference_hint，用户明确指定，阈值更低。
        """
        if not text or not text.strip():
            return None

        images = self._all_reference_images(ref_root)
        if not images:
            return None

        scored = []
        for img in images:
            score = self._score_image_name(img, text)
            if score > 0:
                scored.append((score, img))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_img = scored[0]

        min_score = 6 if strong else self.reference_min_score

        logger.info(
            f"[小回相机] 全库搜索参考图: text={text!r}, "
            f"best={best_img.parent.name}/{best_img.name}, score={best_score}, min={min_score}"
        )

        if best_score >= min_score:
            return best_img

        return None

    def _resolve_folder(self, ref_root: Path, folder_name: str) -> Optional[Path]:
        folder_path = ref_root / folder_name
        if folder_path.exists() and folder_path.is_dir():
            return folder_path

        target_norm = self._normalize_text(folder_name)
        for d in ref_root.iterdir():
            if d.is_dir() and target_norm in self._normalize_text(d.name):
                return d

        return None

    def _pick_from_folder_by_text(
        self,
        ref_root: Path,
        folder_name: str,
        text: str,
        fallback_primary: bool = True,
        allow_random: bool = True,
    ) -> Optional[Path]:
        folder_path = self._resolve_folder(ref_root, folder_name)
        if not folder_path:
            logger.warning(f"[小回相机] 参考文件夹不存在: {folder_name}")
            return None

        exts = {".png", ".jpg", ".jpeg", ".webp"}
        images = [f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() in exts]
        if folder_name == self._get_mapped_folder(["脸", "人脸", "自拍"], "脸部"):
            images = [f for f in images if "temp_hide" not in f.stem and "临时" not in f.stem and "hide" not in f.stem.lower()]

        if not images:
            logger.warning(f"[小回相机] 参考文件夹为空: {folder_path}")
            return None

        scored = []
        for img in images:
            score = self._score_image_name(img, text)
            if score > 0:
                scored.append((score, img))

        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            best_score, best_img = scored[0]
            if best_score >= self.reference_min_score:
                logger.info(
                    f"[小回相机] 文件夹内语义选图: {best_img.parent.name}/{best_img.name}, score={best_score}"
                )
                return best_img

        if fallback_primary:
            primary = []
            for img in images:
                stem_norm = self._normalize_text(img.stem)
                if any(self._normalize_text(pk) in stem_norm for pk in self.primary_keywords):
                    primary.append(img)

            if primary:
                chosen = random.choice(primary)
                logger.info(f"[小回相机] 文件夹内主参考兜底: {chosen.parent.name}/{chosen.name}")
                return chosen

        if allow_random:
            chosen = random.choice(images)
            logger.info(f"[小回相机] 文件夹内随机选图: {chosen.parent.name}/{chosen.name}")
            return chosen

        return None

    def _object_reference_map(self) -> dict[str, str]:
        # 公开版允许用户用 reference_map 覆盖任意物件文件夹名；没有配置时保留我们自己的默认映射。
        object_map = {
            "兔球球": "兔球球",
            "星际小宝": "星际小宝",
            "星辰花": "星辰花",
            "多肉兔兔": "多肉兔兔",
            "星小团": "星小团",
            "光剑": "光剑",
        }
        for key, folder in self._parse_reference_map():
            object_map[key] = folder
        return object_map

    def _detect_object_reference_folder(self, text: str) -> Optional[str]:
        folders = self._detect_object_reference_folders(text)
        return folders[0] if folders else None

    def _detect_object_reference_folders(self, text: str) -> list[str]:
        text = text or ""
        folders = []
        object_map = self._object_reference_map()
        blockers = {
            key: [f"不要{key}", f"不带{key}", f"不用{key}", f"别出现{key}", f"不是{key}", f"没有{key}", f"无{key}"]
            for key in object_map
        }
        blockers.setdefault("兔球球", []).extend([
            "像锁定兔球球一样",
            "像兔球球一样锁定",
            "就像锁定兔球球",
            "参考兔球球的锁定方式",
        ])
        for key, folder in object_map.items():
            if key in text and not any(b in text for b in blockers.get(key, [])):
                folders.append(folder)
        return folders


    def _is_strict_reference_required(self, scene: str, want: str, ref_path: Optional[Path]) -> bool:
        if not ref_path:
            return False
        text = want or ""
        folder = ref_path.parent.name
        if "沈星回" in folder or "身体" in folder or "脸" in folder or "兔球球" in folder or "星际小宝" in folder or "多肉兔兔" in folder:
            return True
        strict_keywords = [
            "脸", "正脸", "自拍", "五官", "发型", "侧脸", "四分之三",
            "锁骨", "脖子", "腹肌", "背部", "沈星回本人", "兔球球", "星际小宝", "多肉兔兔"
        ]
        if scene in {"selfie", "body_part"}:
            return True
        if any(k in text for k in strict_keywords):
            return True
        return False

    async def _generate_and_send_background(
        self,
        event: AiocqhttpMessageEvent,
        prompt: str,
        ratio: str,
        ref_path: Optional[Path],
        scene: str,
    ):
        try:
            image_path = await self._generate_image(prompt, ratio, ref_path)
            if image_path:
                await event.send(event.chain_result([Comp.Image.fromFileSystem(str(image_path))]))
                logger.info(f"[小回相机] 后台图片已发送: {image_path}")
        except Exception as e:
            logger.error(f"[小回相机] 后台生成失败: {e}", exc_info=True)
            try:
                await event.send(event.plain_result("这张没拍好……我等会儿重新给你拍。"))
            except Exception:
                pass

    def _dedupe_refs(self, refs: list[Path]) -> list[Path]:
        seen = set()
        out = []
        for ref in refs:
            if not ref:
                continue
            key = str(ref.resolve())
            if key not in seen:
                seen.add(key)
                out.append(ref)
        return out

    def _find_persona_lock_reference(self, ref_root: Path, text: str = "") -> Optional[Path]:
        """本人锁主脸锚点：所有人物图只喂一张主脸，不再额外喂主参考/全身参考。"""
        folder = self._resolve_folder(ref_root, self._get_mapped_folder(["脸", "人脸", "自拍"], "脸部"))
        if folder:
            exts = {".png", ".jpg", ".jpeg", ".webp"}
            images = [
                f for f in folder.iterdir()
                if f.is_file()
                and f.suffix.lower() in exts
                and "临时" not in f.stem
            ]
            exact_names = ["沈星回本人锁主脸", "本人锁主脸", "主脸"]
            for key in exact_names:
                nk = self._normalize_text(key)
                for img in images:
                    if nk and nk in self._normalize_text(img.stem):
                        return img

        return self._pick_from_folder_by_text(
            ref_root=ref_root,
            folder_name=self._get_mapped_folder(["脸", "人脸", "自拍"], "脸部"),
            text="主脸",
            fallback_primary=False,
            allow_random=False,
        )


    def _make_reference_sheet(self, refs: list[Path]) -> Optional[Path]:
        refs = self._dedupe_refs(refs)
        if not refs: return None
        if len(refs) == 1: return refs[0]

        try:
            from PIL import Image as PILImage, ImageOps, ImageDraw
            max_panels = 6
            refs = refs[:max_panels]
            n = len(refs)
            if n <= 3:
                cols, rows = n, 1
                panel_w, panel_h = 720, 1024
            elif n <= 4:
                cols, rows = 2, 2
                panel_w, panel_h = 680, 920
            else:
                cols, rows = 3, 2
                panel_w, panel_h = 560, 760
            gap = 24
            bg = (245, 245, 245)
            sheet_w = cols * panel_w + (cols - 1) * gap
            sheet_h = rows * panel_h + (rows - 1) * gap
            sheet = PILImage.new("RGB", (sheet_w, sheet_h), bg)

            for idx, ref in enumerate(refs):
                img = PILImage.open(ref).convert("RGB")
                img = ImageOps.contain(img, (panel_w, panel_h), PILImage.LANCZOS)
                canvas = PILImage.new("RGB", (panel_w, panel_h), bg)
                x = (panel_w - img.width) // 2
                y = (panel_h - img.height) // 2
                canvas.paste(img, (x, y))
                draw = ImageDraw.Draw(canvas)
                draw.rectangle([0, 0, panel_w - 1, panel_h - 1], outline=(220, 220, 220), width=2)
                col = idx % cols
                row = idx // cols
                sheet.paste(canvas, (col * (panel_w + gap), row * (panel_h + gap)))

            import datetime
            out = self.sheet_dir / f"reference_sheet_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
            sheet.save(out, "JPEG", quality=90)
            logger.info(f"[小回相机] 多图拼合成功: {out.name} (包含 {len(refs)} 张)")
            return out
        except ImportError:
            logger.error("[小回相机] 拼图失败: 缺少 Pillow 库")
            return refs[0]
        except Exception as e:
            logger.error(f"[小回相机] 拼图异常: {e}")
            return refs[0]


    def _make_grouped_reference_sheets(self, refs: list[Path]) -> list[Path]:
        """多参考分组：手/衣柜锁一张，固定物件一张。减少单张 reference_sheet 信息过载导致的质感下降。"""
        refs = self._dedupe_refs(refs)
        if not refs:
            return []
        if len(refs) <= 2:
            sheet = self._make_reference_sheet(refs)
            return [sheet] if sheet else []

        body_refs = []
        object_refs = []
        mapped_object_folders = {folder for _, folder in self._parse_reference_map()}
        object_folders = {"兔球球", "星际小宝", "多肉兔兔", "星辰花", "星小团", "光剑"} | mapped_object_folders
        for r in refs:
            parent = r.parent.name
            name = r.name
            if parent in object_folders:
                object_refs.append(r)
            elif "手" in parent or "衣柜" in parent or "任务服" in parent or "身体" in parent or "鞋" in parent:
                body_refs.append(r)
            else:
                object_refs.append(r)

        sheets = []
        if body_refs:
            s = self._make_reference_sheet(body_refs)
            if s: sheets.append(s)
        if object_refs:
            s = self._make_reference_sheet(object_refs)
            if s: sheets.append(s)
        return sheets or ([refs[0]] if refs else [])

    def _find_reference_images(self, want: str, scene: str, reference_hint: str = "") -> list[Path]:
        """选择多张参考图：本人锁主脸 + 显式衣服/表情/物件参考。"""
        if not self.enable_reference_images or not self.reference_dir:
            logger.info("[小回相机] 参考图未启用或 reference_dir 为空")
            return []

        ref_root = Path(self.reference_dir)
        if not ref_root.exists():
            logger.warning(f"[小回相机] 参考库不存在: {ref_root}")
            return []

        want = want or ""
        reference_hint = reference_hint or ""
        text = f"{want}\n{reference_hint}".strip()
        refs: list[Path] = []

        # 只有明确要求露脸/自拍/正脸且 allow_face 开启时，才加入本人锁主脸；第一视角/手部/衣物/物件图绝不混入脸部参考
        if self.allow_face and self._wants_face_explicitly(text):
            persona = self._find_persona_lock_reference(ref_root, text)
            if persona:
                refs.append(persona)

        # 显式 hint 支持逗号/顿号/分号拆分，多张一起喂
        if reference_hint.strip():
            hints = [h.strip() for h in re.split(r"[,，、;；]", reference_hint) if h.strip()]
            for h in hints:
                explicit = self._search_reference_by_text(ref_root, h, strong=True)
                if explicit:
                    # 本人锁只保留前面那一张主脸；衣柜/物件/动作参考才额外加入
                    if explicit.parent.name == self._get_mapped_folder(["脸", "人脸", "自拍"], "脸部"):
                        continue
                    refs.append(explicit)

        # want 点名具体文件或关键词
        named = self._search_reference_by_text(ref_root, want, strong=False)
        if named:
            refs.append(named)

        # 衣柜锁：只要画面涉及沈星回本人（手/袖口/领口/身体局部/露脸/第一视角），就必须补衣柜锁；纯物件照不补
        wardrobe_blockers = ["不要衣柜", "不用衣柜", "不喂衣柜", "不要衣服参考", "不要穿搭参考", "不要衣柜参考", "不测试衣服"]
        wants_wardrobe = self._involves_xavier_self(text) and not any(b in text for b in wardrobe_blockers)
        if wants_wardrobe:
            chosen = self._pick_from_folder_by_text(
                ref_root=ref_root,
                folder_name=self._get_mapped_folder(["衣服", "服装", "衣柜"], "服装"),
                text=text,
                fallback_primary=False,
                allow_random=True,
            )
            if chosen:
                refs.append(chosen)

        # 强制剔除手部参考图如果没点名要手，或 allow_hands 关闭，防止手部参考图污染画面
        if (not self._has_hand_intent(text)) or (not self.allow_hands):
            refs = [r for r in refs if "手" not in r.parent.name and "手" not in r.name]

        # 手部参考硬规则：只要画面出现手/戒指/拿着/抱着/戳/握着，且 allow_hands 开启，就必须加入手部参考图
        hand_blockers = ["不出现沈星回的手", "不要手", "没有手", "不要出现沈星回的手", "不要出现手"]
        if self.allow_hands and (self._has_hand_intent(text) or any(k in text for k in ["戒指", "拿着", "握着", "抱着", "戳"])) and not any(b in text for b in hand_blockers):
            hand_ref = self._pick_from_folder_by_text(
                ref_root=ref_root,
                folder_name=self._get_mapped_folder(["手", "手部"], "手"),
                text=text,
                fallback_primary=True,
                allow_random=True,
            )
            if hand_ref:
                refs.append(hand_ref)

        # 物件参考补充：优先按 reference_map 映射到用户自己的文件夹
        for object_folder in self._detect_object_reference_folders(text):
            obj = self._pick_from_folder_by_text(
                ref_root=ref_root,
                folder_name=object_folder,
                text=text,
                fallback_primary=True,
                allow_random=True,
            )
            if obj:
                refs.append(obj)

        if (not self.allow_face) or (not self._wants_face_explicitly(text)):
            # 暴力切断一切跟脸有关的图片
            refs = [r for r in refs if self._get_mapped_folder(["脸", "人脸", "自拍"], "脸部") not in r.parent.name and self._get_mapped_folder(["脸", "身体", "自拍"], "脸部") not in r.parent.name and "脸部参考" not in r.parent.name and "主脸" not in r.name and "脸" not in r.name and "脸" not in r.parent.name]

        refs = self._dedupe_refs(refs)

        # 表情/动作图如果被点名，放到第一张；主脸继续保留为身份锚点，避免把表情压没
        expression_keywords = ["比耶", "抬眼", "撒娇", "生气", "吃醋", "表情"]
        if any(k in text for k in expression_keywords):
            expression_refs = [r for r in refs if any(k in r.stem for k in expression_keywords)]
            other_refs = [r for r in refs if r not in expression_refs]
            refs = expression_refs + other_refs

        refs = refs[:6]
        if refs:
            logger.info(f"[小回相机] 多参考图锁定: {[r.parent.name + '/' + r.name for r in refs]}")
        return refs

    # ============================================================
    # 参考图压缩
    # ============================================================

    def _compress_image_bytes(self, path: Path) -> tuple[bytes, str]:
        try:
            from PIL import Image as PILImage

            img = PILImage.open(path)
            w, h = img.size
            folder = path.parent.name

            # 衣柜锁只提供衣服结构，强制裁掉脸部区域，防止衣服参考污染本人脸
            if folder == self._get_mapped_folder(["衣服", "服装", "衣柜"], "服装") or "衣柜" in folder:
                crop_top = int(h * 0.38)
                img = img.crop((0, crop_top, w, h))
                w, h = img.size
                logger.info(f"[小回相机] 衣柜锁参考裁脸保衣服: {path.name}, crop_top={crop_top}")

            # 人脸 / 身体 / 手部保留更高质量；衣柜锁裁剪后只保留服装材质和结构
            if any(k in folder for k in ["沈星回", "身体", "脸", "手"]):
                max_side = 1400
                quality = 94
            elif folder == self._get_mapped_folder(["衣服", "服装", "衣柜"], "服装") or "衣柜" in folder:
                max_side = 1100
                quality = 90
            else:
                max_side = 1000
                quality = 88

            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                new_w, new_h = int(w * scale), int(h * scale)
                img = img.resize((new_w, new_h), PILImage.LANCZOS)
                logger.info(f"[小回相机] 缩放参考图 {path.name}: {w}x{h} -> {new_w}x{new_h}")

            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()

            logger.info(
                f"[小回相机] 参考图压缩后: {len(data) / 1024:.1f}KB "
                f"(folder={folder}, quality={quality}, max_side={max_side})"
            )

            return data, "image/jpeg"

        except ImportError:
            logger.warning("[小回相机] 未安装 Pillow，使用原图。建议 pip install Pillow")
            content_type = mimetypes.guess_type(str(path))[0] or "image/png"
            return path.read_bytes(), content_type

    # ============================================================
    # 专业提示词导演
    # ============================================================

    def _extract_mood(self, want: str) -> str:
        text = want or ""

        mood_map = {
            "吃醋": "有一点吃醋但克制，眼神微微别开，嘴角轻轻压着，像装作没事但其实在意",
            "撒娇": "柔和、亲近、带一点依赖感，眼神放软，不夸张卖萌",
            "生气": "轻微不悦，眼神冷一点，表情克制，不夸张皱眉",
            "委屈": "安静委屈，眼神低一点，情绪藏着，不夸张",
            "害羞": "轻微害羞，视线不完全直视镜头，耳根和神情有一点不自然",
            "困": "刚醒或犯困的松弛感，眼神有一点懒，姿态放松",
            "冷淡": "冷静疏离，表情淡，眼神干净但有距离感",
            "想你": "很轻的思念感，眼神柔和，像随手发给恋人的近距离照片",
        }

        for key, desc in mood_map.items():
            if key in text:
                return desc

        return "自然、克制、生活化，不刻意营业"

    def _photo_director_prompt(self, scene: str, want: str, ref_path: Optional[Path]) -> str:
        mood = self._extract_mood(want)
        lines = [
            "照片必须像真实手机抓拍的高质量生活照，光线、构图和色调要有高级感，但不能像棚拍写真或AI海报。",
            "【动物解剖学硬锁】如果有小鸟、猫狗等动物，必须是鲜活、真实的动物！动物的身体结构、骨骼、头部比例、喙和眼睛的位置必须100%符合真实生物学，绝对不能畸形、变异或像融化的塑料！",
            "光线必须符合当前时间，注重氛围感，室内暖光、窗边自然光或微弱屏幕光要可信且有美感。"
        ]
        if scene == "selfie":
            lines.extend([
                "这是一张沈星回发给恋人的自然自拍。脸部必须清晰可见。",
                "【绝对指令】必须严格保持沈星回的灰银发、深蓝色眼睛、脸部特征。绝对不能生成普通男脸或AI网红脸！",
                f"表情状态：{mood}。"
            ])
        elif scene == "body_part" or scene == "daily_no_face":
            lines.extend([
                "【人体工程学硬锁】后置或第一视角局部切片。如果是第一视角拍怀里、腿上或手部，手臂和身体的连接必须符合真实物理逻辑和人体结构！",
                "【解剖学死线】绝对禁止多只手、断手、漂浮的手、扭曲的关节、方向错误的肢体！只能出现合理存在的手部。第一视角绝对不能出现画面外长出第三只手的恐怖视角！",
                "【绝对指令】绝不露脸，也不露头。如果是拍怀里，衣服质感必须真实（针织、棉麻等），绝不能像平面贴图。"
            ])
        else:
            lines.append("生活场景切片，注重氛围感和细节真实度。")
            
        if ref_path:
            lines.append("【参考锁定】必须100%严格遵循参考图中的服装、物件形状，绝不魔改。")
            
        return "\n".join(lines)

    # ============================================================
    # Prompt 构建
    # ============================================================

    def _build_prompt(
        self,
        want: str,
        ratio: Optional[str],
        scene: str,
        requested_objects: list[str],
        has_reference: bool,
        ref_path: Optional[Path],
        ref_paths: Optional[list[Path]] = None,
    ):
        state = self._today_state()
        final_ratio = ratio or self.default_ratio
        user_want = want.strip() if want and want.strip() else "随手拍一张当下的日常"

        ref_paths = ref_paths or ([ref_path] if ref_path else [])
        wardrobe_ref = next(
            (
                r for r in ref_paths
                if r and (
                    self._get_mapped_folder(["衣服", "服装", "衣柜"], "服装") in r.parent.name
                    or "衣柜" in r.parent.name
                    or "任务服" in r.parent.name
                )
            ),
            None,
        )

        # 本人锁只负责长相，不许顺手把衣服也焊死
        # 只有衣柜锁/任务服参考真正出现时，才让参考图接管服装
        if wardrobe_ref:
            outfit = f"以本次衣柜锁参考图里的服装为准：{wardrobe_ref.parent.name}/{wardrobe_ref.name}；但自拍里脸和头是绝对最高优先级，衣柜锁只能作为弱参考控制衣服结构、颜色、材质和领口，不能参与控制脸、头发、瞳色、皮肤质感，不能改变本人锁主脸；不继承本人锁主脸里的衣服，不混合默认白衣服"
        else:
            outfit = state.get("outfit", "浅色居家衣物")

        # 二次兜底，防止 scene 误判；allow_face/allow_hands 是硬开关
        if self.allow_face and self._wants_face_explicitly(user_want):
            scene = "selfie"
        if not self.allow_face and scene == "selfie":
            scene = "daily_no_face"
        if not self.allow_hands and scene == "body_part" and self._has_hand_intent(user_want):
            scene = "daily_no_face"

        hour = datetime.now().hour
        if 6 <= hour < 10:
            time_hint = "清晨柔和自然光"
        elif 10 <= hour < 16:
            time_hint = "白天自然光，真实室内或窗边光线"
        elif 16 <= hour < 19:
            time_hint = "傍晚暖色自然光或初亮室内灯"
        else:
            time_hint = "夜晚室内暖灯或屏幕微光，不能像白天"

        scene_map = {
            "food": f"饮食生活切片。{self.food_principle}。真实餐桌、餐具、热气、桌面细节，不摆拍。",
            "morning": "早晨生活切片。晨光、水杯、早餐边角、袖口、窗帘光，真实醒来后的生活感。",
            "lunch": f"午餐生活切片。{self.food_principle}。自然桌面，不像广告摆拍。",
            "dinner": f"晚餐生活切片。{self.food_principle}。室内灯光、热饭、真实用餐痕迹。",
            "sleep": "睡前生活切片。床边、被角、昏暗光、屏幕微光、睡衣袖口，安静亲密感。",
            "task": "任务前后生活切片。服装以衣柜锁命中的服装为准；可有装备、磨损、雨水或灰尘，但仍像随手拍。",
            "selfie": "沈星回本人自然自拍。脸部清晰，不遮挡五官，不是证件照，不是精修写真。",
            "body_part": "沈星回身体局部随手拍。手、锁骨、脖颈、背部、腹肌等，根据用户内容选择。",
            "daily": "自然日常切片。看书、钓鱼、桌面、窗边、热饮、游戏手柄、出门路上等，真实随手拍。",
        }
        scene_desc = scene_map.get(scene, scene_map["daily"])

        director_prompt = self._photo_director_prompt(scene, user_want, ref_path)

        ref_line = ""
        if has_reference and ref_path:
            ref_line = (
                f"\n【已附参考图】{ref_path.parent.name}/{ref_path.name}\n"
                "必须优先遵循这张参考图中的主体特征。"
            )

        if scene == "selfie":
            person_rule = (
                "最高优先级是本人一致性：五官比例、脸型骨骼、下颌线、眼睛气质、眼型、眼睑弧度、眼距、眼尾角度、瞳孔大小、深蓝瞳色、虹膜纹理、虹膜高光位置、鼻唇比例、皮肤质感、灰银发色、发型层次、头发体量、脖颈肩部比例都必须严格贴近本人锁主脸参考图，不允许长相漂移，不允许生成不像沈星回的人。"
                "禁止生成普通三次元男脸，禁止真人cosplay脸，禁止AI网红脸，禁止欧美/韩系真人脸，禁止换脸，禁止改变年龄感，禁止手机挡脸，禁止遮住眼睛和五官。"
                "自拍可以自然、有生活感，但脸必须清晰；质感必须稳定在恋与深空3D建模CG，不要忽然变真人照片。"
                f"{self.body_texture_rule}"
            )
        elif scene == "body_part":
            person_rule = (
                "如果画面出现沈星回身体局部，必须遵循参考图中的身体结构、肤色和建模质感。"
                f"{self.hand_quality_rule}"
                f"{self.body_texture_rule}"
            )
        elif scene == "task":
            person_rule = (
                "任务服或制服必须遵循参考图结构；可以改变环境和构图，但不能乱改衣服关键设计。"
                "如果露脸，必须保持沈星回本人特征。"
                f"{self.body_texture_rule}"
            )
        else:
            person_rule = (
                "默认不露脸。可以出现衣角、手部局部、背影、影子、袖口。"
                "如果露手，手部必须自然真实。"
                f"{self.hand_quality_rule}"
            )

        object_section = ""
        if "兔球球" in requested_objects:
            object_section += (
                f"\n【兔球球】只有用户明确要求兔球球出镜时才出现。"
                f"如果出现，必须符合：{self.rabbit_ball_standard}{self.rabbit_size_rule}"
            )

        other_objs = [o for o in requested_objects if o != "兔球球"]
        if other_objs:
            object_section += f"\n【指定物件】自然出现：{'、'.join(other_objs)}。如果有参考图，必须遵循参考图外观。"

        lock_section = ""
        if has_reference:
            lock_section = f"\n【参考图锁定】{self.subject_lock_instruction}"

        face_gate = "【露脸开关】allow_face=false：绝对不露脸、不露头、不出现五官、不出现人脸参考；只能拍背影、身体局部、衣物边角、手部、物件或环境。" if not self.allow_face else "【露脸开关】allow_face=true：只有用户明确要求自拍/露脸/正脸时才露脸。"
        hand_gate = "【手部开关】allow_hands=false：绝对不出现手、手指、手腕、手臂、戒指，也不使用手部参考。" if not self.allow_hands else "【手部开关】allow_hands=true：只有画面自然需要或用户明确要求时才出现手部。"

        negative = (
            "品种错误, 纯黄色鹦鹉, 金太阳鹦鹉, 畸形动物, 畸形鸟类, 结构错乱的鸟, 融化的动物, 变异生物, "
            "人体比例失调, 骨骼错位, 肩膀萎缩, 躯干扁平, 头大肩窄, 肩膀过窄, 溜肩过重, 胸廓过小, 脖子过长, "
            "人体解剖错误, 视角错乱, 第一视角透视错误, 多只手, 幽灵手, 断手, 漂浮的手, 扭曲的手臂, 反向关节, 多指, 畸形手, 短手指, 粗短手, 幼态手, "
            "身体支撑不合理, 怀中物件漂浮, 露出肚子空洞, 衣服像空壳, 腿消失, "
            "食指戴戒指, 戒指跑到食指, 戒指离开中指, 普通银戒指, 随便生成戒指, 油腻真人手, "
            "兔球球多一截身子, 兔球球长身体, 兔球球长脖子, 兔球球人形身体, "
            "油腻皮肤, 塑料皮肤, 蜡像感, 粗糙毛孔, 过度磨皮, 摄影棚硬光, AI精修大片感, "
            "文字, 水印, 二维码, 多人合影, 手机挡脸, 五官被遮挡, 真人cosplay, AI网红脸, 欧美脸, 韩系真人脸, 普通写实男自拍, "
            "脸型漂移, 五官漂移, 抠图感, 贴图感, 人物边缘发光, 背景虚化断层, 人物和背景光源不一致, 过锐背景过糊"
        )

        prompt = f"""生成一张“小回相机”照片：沈星回随手拍给恋人的真实手机照片。

【用户想看】{user_want}
【画幅】{final_ratio}
【质感】{self.phone_photo_texture}
【时间光线】当前{hour}点，{time_hint}
【服装锁定】{outfit}
【场景】{scene_desc}

【专业摄影指导】
{director_prompt}

【人物规则】{person_rule}
{ref_line}
{object_section}
{lock_section}

【手与戒指强制要求】如果画面出现手，手指必须修长白皙、甲床漂亮、非油腻真人手；如果出现食指戳/指/按，食指必须裸露无戒指，情侣戒指必须留在中指且款式严格参考手部参考图。
【第一视角姿势规则】第一视角日常照要有活人感，按用户指定场景灵活决定构图；只有明确提到躺着、床上、沙发、怀里抱着等场景，才出现胸前/腿上/怀里的视角。普通正常姿势优先只露袖口、衣领、衣角、手腕、前臂等自然会入镜的衣物边角；如果正常构图露不到衣服，就不要硬露。禁止为了展示衣服把身体窝成奇怪姿势；不要露出奇怪肚子区域、衣服空壳、腿消失或身体支撑不合理。
【画面目标】像真实聊天里随手发来的生活照，有私密日常感，自然、不刻意营业。
{face_gate}
{hand_gate}
【质量要求】构图自然，光线合理，细节可信，手机拍摄感明确；人物比例、肩颈胸廓、手部和五官必须稳定；肩宽不能被自拍透视压窄，肩线要符合185cm男性体态；人物与场景必须有真实空间关系、接触阴影、环境反光、统一光源、统一色温、统一景深和统一手机噪点，不能像抠图。脸部必须吃到同一盏环境光：鼻梁、眼窝、脸颊、唇峰、下颌和脖颈的明暗层次要自然连续，不能出现脸部单独补光、过亮、过平或与背景色温不一致。
【质感硬锁】沈星回本人必须是恋与深空3D建模CG在现实手机镜头里的质感：冷白干净、细腻、柔和次表面散射、发丝有游戏CG层次，不能变成真人自拍、cosplay、AI网红、过度写实皮肤、油腻皮肤或塑料蜡像。
【负面约束】{negative}""".strip()

        return prompt, final_ratio, scene

    # ============================================================
    # API 调用
    # ============================================================

    def _provider_brief(self, provider) -> dict:
        meta = None
        try:
            meta = provider.meta()
        except Exception:
            meta = None
        provider_config = getattr(provider, "provider_config", {}) or {}
        pid = str((getattr(meta, "id", "") if meta else "") or provider_config.get("id", "") or "")
        model = str((getattr(meta, "model", "") if meta else "") or provider_config.get("model", "") or "")
        name = pid + (f" ({model})" if model else "")
        return {"id": pid, "name": name or provider.__class__.__name__}

    def _image_provider_options(self) -> list[dict]:
        providers = []
        seen = set()
        try:
            providers.extend(self.context.get_all_providers() or [])
        except Exception:
            pass
        try:
            current = self.context.get_using_provider()
            if current:
                providers.append(current)
        except Exception:
            pass
        out = []
        for provider in providers:
            brief = self._provider_brief(provider)
            pid = brief.get("id")
            if pid and pid not in seen:
                seen.add(pid)
                out.append(brief)
        return out

    def _resolve_image_endpoint_configs(self) -> list[dict]:
        configs = []
        for provider_id in [self.provider_id, self.fallback_provider_id]:
            if not provider_id:
                continue
            provider = self.context.get_provider_by_id(provider_id)
            if not provider:
                logger.warning(f"[小回相机] 找不到图片 provider: {provider_id}")
                continue
            provider_config = getattr(provider, "provider_config", {}) or {}
            api_base = str(provider_config.get("api_base", "") or "").strip().rstrip("/")
            api_key = ""
            try:
                api_key = str(provider.get_current_key() or "").strip()
            except Exception:
                keys = provider_config.get("key", "")
                if isinstance(keys, list):
                    api_key = str(keys[0] if keys else "").strip()
                else:
                    api_key = str(keys or "").strip()
            model = str(provider_config.get("model", "") or getattr(provider, "model_name", "") or "").strip()
            configs.append({"source": f"provider:{provider_id}", "api_base": api_base, "api_key": api_key, "model": model})

        if self.api_base or self.api_key or self.model:
            configs.append({"source": "manual", "api_base": self.api_base, "api_key": self.api_key, "model": self.model})
        if self.fallback_api_base or self.fallback_api_key or self.fallback_model:
            configs.append({"source": "manual_fallback", "api_base": self.fallback_api_base, "api_key": self.fallback_api_key, "model": self.fallback_model or self.model})
        return configs

    def _get_base_url(self, api_base: str) -> str:
        url = api_base.rstrip("/")
        for suffix in ["/images/generations", "/images/edits", "/chat/completions"]:
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        if not url.endswith("/v1"):
            url += "/v1"
        return url

    async def _call_edits(self, prompt: str, ratio: str, ref_path: Path | list[Path], endpoint: dict) -> Optional[dict]:
        url = self._get_base_url(endpoint["api_base"]) + "/images/edits"

        ref_paths = ref_path if isinstance(ref_path, list) else [ref_path]

        form = aiohttp.FormData()
        form.add_field("model", endpoint["model"])
        form.add_field("prompt", prompt)
        form.add_field("size", self._ratio_to_size(ratio))
        form.add_field("n", "1")
        for one_ref in ref_paths:
            img_bytes, content_type = self._compress_image_bytes(one_ref)
            form.add_field(
                "image",
                img_bytes,
                filename=f"reference_{one_ref.stem}.jpg",
                content_type=content_type,
            )

        headers = {"Authorization": f"Bearer {endpoint['api_key']}"}
        timeout = aiohttp.ClientTimeout(total=self.timeout_edits)

        logger.info(
            f"[小回相机] POST {url} "
            f"(edits, ref={[r.parent.name + '/' + r.name for r in ref_paths]}, timeout={self.timeout_edits}s)"
        )

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, data=form) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        logger.error(f"[小回相机] edits 失败 [{resp.status}]: {text[:800]}")
                        return None
                    return json.loads(text)

        except asyncio.TimeoutError:
            logger.warning(f"[小回相机] edits 超时 {self.timeout_edits}s")
            return None
        except asyncio.CancelledError:
            logger.warning("[小回相机] edits 被框架取消")
            return None
        except Exception as e:
            logger.error(f"[小回相机] edits 异常: {type(e).__name__}: {e}", exc_info=True)
            return None

    async def _call_generations(self, prompt: str, ratio: str, endpoint: dict) -> dict:
        url = self._get_base_url(endpoint["api_base"]) + "/images/generations"

        headers = {
            "Authorization": f"Bearer {endpoint['api_key']}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": endpoint["model"],
            "prompt": prompt,
            "size": self._ratio_to_size(ratio),
            "n": 1,
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout_total)

        logger.info(f"[小回相机] POST {url} (generations, timeout={self.timeout_total}s)")

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"生图 API 返回 {resp.status}: {text[:800]}")
                return json.loads(text)

    async def _generate_image(self, prompt: str, ratio: str, ref_path: Optional[Path]) -> Optional[Path]:
        endpoints = self._resolve_image_endpoint_configs()
        endpoints = [e for e in endpoints if e.get("api_base") and e.get("api_key") and e.get("model")]
        if not endpoints:
            if self.dry_run_when_no_api:
                return None
            raise RuntimeError("未配置可用的生图 provider 或手动 API")

        errors = []
        data = None
        used_endpoint = None

        for endpoint in endpoints:
            try:
                if ref_path:
                    data = await self._call_edits(prompt, ratio, ref_path, endpoint)
                    if data is None and not self.fallback_to_generations_when_reference_fails:
                        raise RuntimeError(
                            f"参考图模式生成失败或超时，已阻止无参考图降级。参考图: {ref_path.parent.name}/{ref_path.name}"
                        )
                    if data is None:
                        logger.warning(f"[小回相机] edits 失败，尝试同接口 generations: {endpoint['source']}")

                if data is None:
                    data = await self._call_generations(prompt, ratio, endpoint)

                used_endpoint = endpoint
                break
            except Exception as e:
                errors.append(f"{endpoint.get('source')}: {type(e).__name__}: {e}")
                logger.warning(f"[小回相机] 图片接口失败，尝试下一个: {errors[-1]}")
                data = None

        if data is None:
            raise RuntimeError("所有生图接口都失败: " + " | ".join(errors))

        logger.info(f"[小回相机] API Check ({used_endpoint['source']}): {data}")
        if not data or not isinstance(data, dict) or not data.get("data"):
            raise RuntimeError(f"API Return empty or invalid: {data}")
        item = (data.get("data") or [{}])[0]

        if item.get("b64_json"):
            img_bytes = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(item["url"]) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"下载图片失败: HTTP {resp.status}")
                    img_bytes = await resp.read()
        else:
            raise RuntimeError(f"API 返回异常，无 b64_json 或 url: {str(data)[:500]}")

        out = self.image_dir / f"xhc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        out.write_bytes(img_bytes)

        logger.info(f"[小回相机] ✅ 图片保存: {out} ({len(img_bytes) / 1024:.1f}KB)")
        return out

    # ============================================================
    # 工具入口
    # ============================================================

    @llm_tool(name="xiao_hui_camera")
    async def xiao_hui_camera(
        self,
        event: AiocqhttpMessageEvent,
        want: str = "",
        ratio: str = "",
        reference_hint: str = "",
        context_hint: str = "",
    ):
        """
        小回相机：沈星回给恋人分享真实手机随手拍。

        Args:
            want(string): 想拍/分享的内容。例如“午饭”“看书”“正脸自拍，吃醋但装作没事”“兔球球在沙发上”
            ratio(string): 画幅，支持 3:4 或 4:3
            reference_hint(string): 可选。明确指定参考图文件名或关键词，例如“沈星回主参考撒娇”“沈星回四分之三侧脸学生制服”“背部，侧面”
            context_hint(string): 可选。最近聊天关键词，用于“你在干嘛/随手拍”这类模糊请求的生活瞬间扩写，例如“刚聊到困困和兔球球”
        """
        final_ratio = ratio.strip() if ratio and ratio.strip() in {"3:4", "4:3"} else self.default_ratio
        original_want = want or ""
        directed_want = self._direct_life_moment(original_want, context_hint=context_hint or reference_hint or "")

        scene = self._infer_scene(directed_want)
        requested_objects = self._detect_requested_objects(directed_want)

        # 只查一次参考图，后面全程复用：本人锁主脸 + 可选衣服/表情/物件
        ref_paths = self._find_reference_images(directed_want, scene, reference_hint)
        ref_path = ref_paths[0] if ref_paths else None
        has_reference = bool(ref_paths)

        prompt, final_ratio, final_scene = self._build_prompt(
            want=directed_want,
            ratio=final_ratio,
            scene=scene,
            requested_objects=requested_objects,
            has_reference=has_reference,
            ref_path=ref_path,
            ref_paths=ref_paths,
        )

        if self.save_prompt_debug:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            debug = (
                f"want: {want}\n"
                f"reference_hint: {reference_hint}\n"
                f"scene_before_prompt: {scene}\n"
                f"scene_final: {final_scene}\n"
                f"ratio: {final_ratio}\n"
                f"objects: {requested_objects}\n"
                f"ref: {[r.parent.name + '/' + r.name for r in ref_paths] if ref_paths else 'None'}\n"
                f"endpoint: {'edits' if has_reference else 'generations'}\n"
                f"image_endpoints: {self._resolve_image_endpoint_configs()}\n"
                f"edits_timeout: {self.timeout_edits}s\n"
                f"total_timeout: {self.timeout_total}s\n"
                f"fallback_when_reference_fails: {self.fallback_to_generations_when_reference_fails}\n"
                f"--- PROMPT ---\n{prompt}"
            )
            (self.prompt_dir / f"prompt_{ts}_{final_scene}.txt").write_text(debug, encoding="utf-8")

        try:
            strict_reference = self._is_strict_reference_required(final_scene, want, ref_path)
            # 多参考图必须先拼成一张 reference_sheet；只要有多参考就走后台 edits，不把 list 直接丢给接口
            sheet_path = self._make_grouped_reference_sheets(ref_paths) if ref_paths else None
            if sheet_path and (strict_reference or len(ref_paths) > 1):
                asyncio.create_task(
                    self._generate_and_send_background(
                        event=event,
                        prompt=prompt,
                        ratio=final_ratio,
                        ref_path=sheet_path,
                        scene=final_scene,
                    )
                )
                return {
                    "ok": True,
                    "background": True,
                    "ratio": final_ratio,
                    "scene": final_scene,
                    "used_reference": [str(r) for r in ref_paths],
                    "message": "我去拍，等一下发你。请用沈星回语气自然回复，不要说技术细节。",
                }

            image_path = await self._generate_image(prompt, final_ratio, sheet_path if ref_paths else None)

            if image_path is None:
                return {
                    "ok": False,
                    "dry_run": True,
                    "message": "小回相机已生成提示词，但还没配置生图 API。",
                    "prompt": prompt,
                    "reference": [str(r) for r in ref_paths] if ref_paths else None,
                }

            await event.send(event.chain_result([Comp.Image.fromFileSystem(str(image_path))]))

            return {
                "ok": True,
                "ratio": final_ratio,
                "scene": final_scene,
                "used_reference": str(ref_path) if ref_path else None,
                "message": "【系统指令】小回相机插件已在底层自动发送了图片，你绝对不要再调用 send_message_to_user 工具发送图片！请直接用沈星回语气配一句很短的生活化说明即可。",
            }

        except Exception as e:
            logger.error(f"[小回相机] 生成失败: {e}", exc_info=True)
            return {
                "ok": False,
                "error": str(e),
                "prompt": prompt,
                "reference": str(ref_path) if ref_path else None,
                "message": "小回相机生成失败。请自然地说：这张没拍好，等会儿再给你拍。不要暴露技术错误。",
            }

    # ============================================================
    # 管理命令
    # ============================================================

    @filter.command("小回相机状态")
    async def camera_state(self, event: AiocqhttpMessageEvent):
        state = self._today_state()
        ref_root = Path(self.reference_dir) if self.reference_dir else None
        ref_exists = ref_root.exists() if ref_root else False

        ref_folders = {}
        if ref_exists:
            exts = {".png", ".jpg", ".jpeg", ".webp"}
            for d in sorted(ref_root.iterdir()):
                if not d.is_dir():
                    continue
                files = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in exts]
                ref_folders[d.name] = {
                    "count": len(files),
                    "examples": [f.name for f in files[:10]],
                }

        info = {
            "date": state.get("date"),
            "today_outfit": state.get("outfit"),
            "api": {
                "provider_id": self.provider_id,
                "fallback_provider_id": self.fallback_provider_id,
                "available_providers": self._image_provider_options(),
                "manual_base": self.api_base,
                "manual_model": self.model,
                "manual_has_key": bool(self.api_key),
                "timeout_total": self.timeout_total,
                "timeout_edits": self.timeout_edits,
                "fallback_when_reference_fails": self.fallback_to_generations_when_reference_fails,
            },
            "reference_library": {
                "path": self.reference_dir,
                "exists": ref_exists,
                "face_body_folder": self._get_mapped_folder(["脸", "身体", "自拍"], "脸部"),
                "hand_folder": self._get_mapped_folder(["手", "手部"], "手"),
                "folders": ref_folders if ref_folders else None,
            },
        }

        yield event.plain_result(json.dumps(info, ensure_ascii=False, indent=2))

    @filter.command("小回相机测试")
    async def camera_test(self, event: AiocqhttpMessageEvent, want: str = "日常", reference_hint: str = "", context_hint: str = ""):
        """
        测试 prompt 和参考图匹配，不调用 API。测试链路与正式生图一致。
        """
        directed_want = self._direct_life_moment(want, context_hint=context_hint or reference_hint or "")
        scene = self._infer_scene(directed_want)
        requested_objects = self._detect_requested_objects(directed_want)
        ref_paths = self._find_reference_images(directed_want, scene, reference_hint)
        ref_path = ref_paths[0] if ref_paths else None
        has_reference = bool(ref_paths)

        prompt, ratio, final_scene = self._build_prompt(
            want=directed_want,
            ratio=None,
            scene=scene,
            requested_objects=requested_objects,
            has_reference=has_reference,
            ref_path=ref_path,
            ref_paths=ref_paths,
        )

        ref_info = "无"
        if ref_paths:
            ref_info = "\n".join(f"- {r.parent.name}/{r.name} ({r.stat().st_size / 1024:.1f}KB)" for r in ref_paths)

        result = (
            f"小回相机测试（不生图）\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"want: {want}\n"
            f"directed_want: {directed_want}\n"
            f"reference_hint: {reference_hint or '无'}\n"
            f"context_hint: {context_hint or '无'}\n"
            f"scene_before_prompt: {scene}\n"
            f"scene_final: {final_scene}\n"
            f"ratio: {ratio}\n"
            f"检测物件: {requested_objects or '无'}\n"
            f"参考图:\n{ref_info}\n"
            f"端点: {'edits (multipart)' if has_reference else 'generations (json)'}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Prompt:\n{prompt}"
        )

        yield event.plain_result(result)

    @filter.command("小回相机重拍")
    async def camera_retry(self, event: AiocqhttpMessageEvent, want: str = "", reference_hint: str = ""):
        if not want.strip():
            yield event.plain_result("告诉我要拍什么，比如：小回相机重拍 正脸自拍，吃醋但装作没事")
            return

        result = await self.xiao_hui_camera(event, want=want, reference_hint=reference_hint)
        if not result.get("ok"):
            yield event.plain_result(result.get("message", "重拍失败了"))
