# 公开运维语料

## 范围

首版语料只收录许可证清晰、可固定到不可变 Git 提交的官方 Markdown 文档。它用于验证摄取合同和检索基线，不等同于企业内部 Runbook、事故复盘或生产 SOP。

| 来源 | 固定提交 | 文档数 | 许可证 |
| --- | --- | ---: | --- |
| Kubernetes Website | `22cc5093581b4dc74394bdce60753d5caf13ddf0` | 10 | CC-BY-4.0 |
| Prometheus | `0923fdef23c3468ce4afdb3f2cb6c14a79037214` | 10 | Apache-2.0；仓库含 NOTICE |

选择这两组材料是因为它们与 Pod、Service、探针、Prometheus 查询和运维排障相关，来源与许可也能在同一个固定提交中核验。完整文档清单、文件哈希、许可 URL 和署名位于 [`public-ops-v1.json`](../src/enterprise_agent/corpus_manifests/public-ops-v1.json)。

## 获取与离线重建

首次构建需要访问两个 HTTPS Git 仓库：

~~~bash
PYTHONPATH=src python -m enterprise_agent corpus-build \
  --manifest src/enterprise_agent/corpus_manifests/public-ops-v1.json \
  --cache-dir work/corpus-cache \
  --output work/corpora/public-ops-v1.json
~~~

成功后可以完全离线重建：

~~~bash
PYTHONPATH=src python -m enterprise_agent corpus-build \
  --manifest src/enterprise_agent/corpus_manifests/public-ops-v1.json \
  --cache-dir work/corpus-cache \
  --output work/corpora/public-ops-v1.json \
  --offline --overwrite
~~~

当前冻结身份：

- manifest SHA-256：`6543664fba85eecd4a4bd4514689622c8ba98bb2a321e8dff7b3d893360055d9`
- corpus SHA-256：`d1dd9071a9833c6c985c3e9b939acac95c954e2167b5fb3cc3e9b96fe947100f`
- corpus version：`public-ops-docs:1.0.0`

构建器直接读取固定提交中的 Git object，不依赖工作树；先校验 blob 类型和大小，再校验 LICENSE/NOTICE 与每篇正文的 SHA-256。生产 manifest 只允许规范 HTTPS remote，并隔离用户 Git 配置和外部协议。frontmatter 被确定性移除，正文保留 Markdown，随后按标题切分。

`effective_at` 表示本次来源快照的 commit 时间，不表示上游文档声明的业务生效时间。生成元数据中的 `modifications` 明确记录了 frontmatter 移除、换行规范化与 JSON 封装。

## 存储、许可与再分发

- `work/corpus-cache/` 和 `work/corpora/` 已被 Git 忽略，避免把第三方全文或裸仓库缓存提交进本项目。
- 提交到仓库的是 manifest、评测 qrels 和默认脱敏报告；报告中的结果不含正文。
- 生成的 corpus metadata 保存 SPDX、许可 URL、许可文件哈希、署名、NOTICE URL 与修改说明，但不会把 LICENSE/NOTICE 正文嵌入每篇文档。
- 如果要再分发生成后的完整 corpus，必须同时满足各上游许可证，并随分发物附上相应 LICENSE、署名和 Prometheus NOTICE；不能只依赖本项目报告中的 URL。

这不是法律意见。新增来源前应先确认许可允许目标使用与再分发；许可证不清晰的博客、事故复盘或公司状态页默认不摄取。

## 评测集

[`public-ops-retrieval-v1.json`](../src/enterprise_agent/retrieval_suites/public-ops-retrieval-v1.json) 包含 44 条 case：

- 40 条正例：每篇文档各一条英文和中文查询；
- 4 条范围隔离负例：跨 tenant 或不存在的 product filter；
- 0 条同 tenant、同 product 的语义 hard negative。

因此 `scope_isolation_accuracy` 与 `semantic_no_answer_accuracy` 分开报告。当前前者为 1.0，后者为 `null`，不能把范围过滤的成功解释成语义安全拒答。

查询由来源文档内容导出，尚未独立出题、盲审或做 pooled judgment；每条正例只标一个 relevant document。该 suite 的正式定位是 source-derived draft smoke set，不是独立 held-out 质量证明。

## 后续可引入的数据类型

- 通用检索或多语言 benchmark（例如 [BEIR](https://github.com/beir-cellar/beir)、[MIRACL](https://github.com/project-miracl/miracl)）适合验证检索器能力，但与企业运维知识域有明显分布差异，应单独报告。
- 公共日志与 AIOps 数据适合日志解析、异常检测和工具评测，不应直接伪装成 Runbook 文档语料。
- 公开 postmortem、状态页和工程博客更贴近事故场景，但许可经常不够明确；只有在逐项确认许可、版本与再分发要求后才加入 manifest。

下一步不是继续扩大未审核正文，而是让独立评审者编写问题和 graded qrels，增加同域 hard negative、多相关文档 pooled judgment，并冻结不参与调参的 held-out 集。
