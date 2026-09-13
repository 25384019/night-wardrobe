# NovelAI 法典配方库导入计划

> 目标：把三份《所长 NovelAI 个人法典》作为可浏览、可复制的完整 Prompt 配方导入夜之主衣柜；不再把句内片段误当作独立 Tag。

## 1. 导入模型与幂等性

**Files:** `tag_manager/db.py`, `tag_manager/import_novelai_codex.py`, `tag_manager/tests/test_novelai_codex_import.py`

1. 为 `recipes` 添加可选的稳定 `import_key`，建立唯一索引，保留已有手工配方的兼容性。
2. 编写 DOCX 解析器：以中文标题/章节为上下文，连续英文 Prompt 段合并成一条配方；保留原文换行、来源文件和章节路径。
3. 使用 `来源文件 + 章节路径 + 条目序号` 生成稳定 key，重复运行时更新同一条配方，不制造副本。
4. 将导入条目写为 `type=codex_prompt`；导入前创建 SQLite 备份，导入后输出总数、各文件数量和跳过原因。
5. 编写临时数据库测试，覆盖标题与 Prompt 合并、重复导入、中文内容保留，以及不生成 Tag 记录。

## 2. 配方库浏览与工作台隔离

**Files:** `tag_manager/app.py`, `tag_manager/templates/recipes.html`, `tag_manager/tests/test_recipe_archive.py`

1. 注册“法典 Prompt”配方类型，并在配方库按类型、关键词、页码查询；每页只读取有限条记录。
2. 保持手工添加、编辑、删除的现有接口与默认筛选行为。
3. 默认不把法典归档加载进工作台/`/api/recipes` 的全部配方数据；只有明确请求 `codex_prompt` 时才返回，避免大型归档拖慢工作台。
4. 在配方库显示当前页、总数、上一页/下一页，并将查询参数保留在翻页链接中。
5. 添加路由测试，验证归档可分页查询、工作台默认不加载归档、显式按类型时可以取得。

## 3. 实际导入与核验

**Files:** `tag_manager/tag_wardrobe.sqlite3`（用户数据）、`tag_manager/tag_wardrobe.before-codex-recipe-import.<timestamp>.sqlite3`（备份）

1. 在现有运行环境执行导入器，输入三份用户提供的 DOCX，不访问网络。
2. 以 SQLite 查询和本地 API 核验：Tag 库仍为 SD WebUI 的 140,782 条；配方库出现三份法典的完整 Prompt，中文标题和来源可见。
3. 重跑一次 dry-run/幂等性核验，确认记录数不增长。
4. 运行相关单元测试并报告导入数、跳过数、备份文件位置。
