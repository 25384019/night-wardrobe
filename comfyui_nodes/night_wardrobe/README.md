# 夜之主衣柜 ComfyUI 节点

将本目录复制到 ComfyUI 的 `custom_nodes/night_wardrobe` 后重启 ComfyUI。

节点分类为“夜之主衣柜”，包含提示词组装、LoRA 加载、保存并记录图源三个节点。保存节点写入 `night_wardrobe_*` PNG 元数据，Night Wardrobe 会在增量扫描时保留原始 Prompt，并填充 `artist_tokens`、`character_tokens`、`other_tags` 与 `style_unit_json`。

本节点包只访问本机数据库、模型目录和输出目录，不发起网络请求。
