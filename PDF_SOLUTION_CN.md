# 财报 PDF 全文解析与证据页检索

本地仓库：`F:\vs_space\powercontext`。参赛题号：`source-03-pdf-retrieval`（赛题四，50 分）。实现位于 `src/powercontext/sources/pdf_pages.py`，补充行为测试位于 `tests/test_pdf_pages.py`。

本分支基于官方 `2026-competition` 分支，保留冻结基线 `9db1753434162c0aa7c9f1189a1722e617880a52` 的完整祖先关系。没有修改官方公开测试、样例或评分逻辑。

## 实现

- 使用 pypdf 从完整原始 PDF 字节解析全部物理页，保留空白页、表格数字和原始页文本；物理页码从 1 开始。
- 验证 PDF 文件头、完整结束标记、可读取的空密码权限及页树完整性。解析全部完成后才返回 tuple，失败统一抛出 ValueError。
- 每页 Source 为 CAPTURED；身份使用文档 ID、完整 PDF SHA-256 和物理页码组成的规范元组摘要，名称长度固定，PDF 修订不会覆盖旧身份。
- 标准投影严格包含 `DOCUMENT_ID\nPAGE_TEXT` 及三个规定的 metadata 字段。
- 检索只使用公开的 scoped Memory `search` 和 `list` API。对真实、活跃的 PDF 页面条目进行 FTS、BM25、重叠段落评分、通用财务词汇扩展和报表标题排序，返回原存储条目的精确 MemoryCitation。
- 检索扫描当前 scope；不依赖跨调用缓存、在线模型、预解析正文、答案、gold 或固定文档页码。扫描成本随当前 scope 的文本量线性增长，适用于本题规模。

## 本地验证

已在 WSL Ubuntu x86_64、Python 3.12、pypdf 6.19.0 及本题使用的固定运行依赖下验证：

- 官方公开测试：6 项通过，完整解析并真实持久化 PepsiCo 503 页，重启后完成 5 个自然语言问题的引用回查。
- 补充行为测试：6 项通过，覆盖文本数字与空页、空 ID、密码保护、截断 EOF、不完整页树、长 ID 与修订身份、财务同义词、同时召回利润表与现金流量表、scope 隔离及 limit。

新增文件的 Ruff 检查、格式检查及 ty 类型检查通过。按仓库规范执行过 `uv run prek run -a`；全仓检查未通过，原因包括基线中其他赛题尚未实现的接口、公开资产的既有格式问题及 Windows 不支持的 Unix API 类型检查。工具对其他题目公开文件的自动格式化已还原，提交仅包含本题源码、补充测试和本说明。

公开问题的单次检索在本机约 0.3–0.5 秒，仅供性能参考。公开测试不提供质量 gold，也不直接检查官方证据页 Recall@20；隐藏集质量、Recall@20 门槛和最终得分须由正式评测确认。

本机复测命令（PowerShell）：

```powershell
wsl -e sh -lc 'cd /mnt/f/vs_space/powercontext && PYTHONPATH=src:competition PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /home/jnysw/.venvs/powercontext-pdf/bin/python -m pytest -q -s -c competition/pytest.ini --confcutdir=competition competition/challenges/source-03-pdf-retrieval/acceptance /mnt/f/vs_space/powercontext/tests/test_pdf_pages.py'
```

在其他 Linux 机器上，按照 `competition/README.md` 安装依赖后运行：

```bash
make -C competition install
make -C competition check-baseline
make -C competition test-pdf
```

## 提交步骤

1. 在 GitHub 创建自己的私有 PowerContext 参赛仓库，或使用已有的参赛仓库。保留当前本地 Git 历史，不要只复制源码后重新 `git init`。
2. 将本地完成的分支推送到该仓库。将下面 URL 替换成你自己的仓库地址；不要推送到官方上游：

   ```powershell
   Set-Location F:\vs_space\powercontext
   git remote add submission https://github.com/YOUR_NAME/YOUR_REPOSITORY.git
   git push submission pdf-retrieval
   git rev-parse HEAD
   ```

   如果已经存在名为 `submission` 的 remote，用 `git remote set-url submission ...` 更新地址。

3. 打开[大赛测试平台](https://open.oceanbase.com/train/detail/21)，登录参赛账号，在参赛信息中配置自己的 PowerContext 仓库，并按平台要求授予评测端读取权限。私有仓库的认证信息只填写到平台指定的私密位置，不提交到源码或文档中。
4. 提测时选择“赛题四：财报 PDF 全文解析与证据页检索”，填写自己的仓库 HTTPS URL、分支 `pdf-retrieval`、`git rev-parse HEAD` 输出的完整 40 位 commit SHA。分支与 SHA 必须已经推送，两个字段不能填反。
5. 提交后等待平台评测结果。提交 PR 到官方仓库不等于提交本题；本地测试通过也不会自动上报成绩。

如果使用本地交付的 `.bundle` 备份，可先运行 `git clone -b pdf-retrieval F:\vs_space\powercontext-pdf-solution.bundle F:\vs_space\powercontext-submit` 恢复完整仓库，再配置自己的远端并推送。

题面与提交依据：[赛题四](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/ohy6lqb9wshgmrar)、[官方自测说明](https://github.com/oceanbase/powercontext/blob/2026-competition/competition/README.md)、[GitHub 参赛说明](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/qs1povt8zbza6hts)。
