# 标注复核怎么用

## 打开

```
python evals/knowledge_v2/open_review.py
```

它会起一个本地服务并打开页面。请用这种方式，不要直接双击 `index.html`。

原因：直接双击打开时地址是 `file:`，部分浏览器会禁止页面保存进度，那样刷新一次批注就没了。页面在这种情况下会显示红色提示，把批注导出再刷新是唯一的保住方式。

## 你要做的三件事

每条草稿上：

1. **判通过 / 不通过 / 拿不准**
   - `通过` 表示这条是应该长期保留的知识，而且草稿的表述可以用（或你已经在下面改好了）
   - `不通过` 表示这条不该进知识层，例如它只是宣传语、通用背景、或者材料并不支持它
   - `拿不准` 表示需要再看一版或再想一下。拿不准**不是**通过，它不会进标注集

2. **改正后的表述**（可选）。草稿的这句话是模型自己写的，不是标注。如果你觉得该留下但说法不对，在这里写对的说法，它会替换掉草稿的表述。

3. **批注**（可选但很有用）。写清为什么。这批批注会随标注一起留下，将来别人看这条为什么通过时只有你的这句话可查。

另外有一个 `这是零容忍错误` 的勾选框。勾上表示这条属于规格里"即使总指标达标也不放行"的那一类，例如把提议写成已采纳的决定、把系统推导的理由写成当时原话、伪造原文引用。这个勾选是独立的：一条可以既不通过又被标为零容忍。

## 快捷键

`↑` / `↓` 或 `j` / `k` 切换条目，`1` 通过，`2` 不通过，`3` 拿不准。

## 做完之后

点右下角 **导出批注**，浏览器会下载 `review-annotations.json`。把这个文件交回来，我会：

```
python evals/knowledge_v2/apply_review.py --annotations review-annotations.json --write
```

这条命令把通过的写成 `human_confirmed`，把不通过的记进 `gold.rejected.jsonl` 连同你的批注（不删除，否则这份标注的历史就查不到了），把拿不准的列出来等第二遍，把零容忍的写进 `critical_failures.jsonl`。

之后：

```
python evals/knowledge_v2/run_eval.py --check-gold
```

会报告标注集的规模离发布门槛还差多少。在没有人确认之前，评测拒绝输出质量报告，因为拿一份未经确认的标注算出来的数字是伪造的度量。

## 这批草稿是怎么来的

```
python evals/knowledge_v2/build_corpus.py --material-root "/e/AI infra" --write   # 材料组与哈希
python evals/knowledge_v2/screen_material.py --root "/e/AI infra"                 # 凭据形态筛查
python evals/knowledge_v2/draft_gold.py --material-root "/e/AI infra" --write     # 跑流水线出草稿
python evals/knowledge_v2/build_review_page.py                                    # 生成本页
```

`expected_meaning` 抄自模型自己的 statement。这是这批草稿最需要你动的地方，因为拿它对模型打分等于让模型给自己判卷。
