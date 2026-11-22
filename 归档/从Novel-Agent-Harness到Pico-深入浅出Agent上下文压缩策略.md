# 从Novel Agent Harness到Pico：深入浅出Agent 上下文压缩策略

## 导读

大语言模型的一切推理能力都建立在一个前提之上：上下文窗口。Code Agent在每一轮对话中，都需要将完整的消息历史（系统提示、用户消息、助手回复、工具调用与结果）打包发送给模型。随着对话推进，这个历史不可避免地膨胀，最终撞上模型上下文窗口的天花板。

可以把上下文窗口想象成一块有限大小的白板。Agent 的所有工作记忆——对话历史、工具结果、中间推理——都必须写在这块白板上。白板空间用完时，你必须擦掉旧内容才能写新内容。关键问题是：擦掉什么、保留什么、用什么方式擦？

这不仅是 Code Agent面对的问题，而是所有长对话 Agent 系统都必须解决的核心工程挑战。许多 Agent 框架在这个问题上选择了"暴力截断"——直接丢弃最早的消息。

## 最简单的上下文压缩

![该图片是最简单的上下文压缩策略的流程示意图，对应文档里的相关内容。它展示了该策略的运作逻辑：每轮结束后，先判断上下文是否达到上限；如果未达到上限，就什么都不做；如果达到了上限，就调用LLM做摘要全量替换历史。这一流程直观呈现了该策略"未超限则保留、超限则全量摘要替换"的简单核心规则。](https://feishu.cn/file/YJgSbDsNgoI3Oyx6cBTcv5cPnLg)

最简单的上下文压缩策略是在达到上下文上限的时候，调用LLM做摘要全量替换历史，否则什么都不做。

这种做法很简单直观，但也很暴力，会带来以下问题：

1. **悬崖式触发压缩：**

比如 Agent 连续读了十几个文件、跑了几轮测试、改了两版代码，前面所有历史都原样塞进 prompt。只要没超限，系统就不动；一旦超限，就突然把几十轮对话压成一段摘要。

这对大模型很不友好。模型虽然能接收长上下文，但注意力不是均匀分配的。上下文越长，越容易出现"看得到但用不好"的情况：关键约束被淹没在大量工具输出里，**早期用户要求在后续推理中权重变低，模型更容易抓住最近几轮的表面信息，而不是任务真正的主线。也就是说，没超限不等于上下文健康，只是还没撞到硬上限。**

悬崖式触发压缩会让问题更严重。压缩前，模型已经在过长上下文里丢失了注意力；压缩后，模型又突然失去大量过程信息。它前一轮还看到完整测试日志，下一轮可能只剩一句"测试失败，已尝试修复"。**这种上下文断崖会让 Agent 的行为变得不稳定：要么重复读文件找回状态，要么基于不完整摘要继续做判断。**

2. **全量摘要不可避免地会丢失细节**：这个问题比较直观，Agent在长对话中，工具调用，用户请求等关键信息，如果只是被大模型做全量的摘要，那他就会丢失他当前任务最需要的关键信息，比如变量名、用户的具体请求、错误信息等。没了这些，Agent干活的质量会差很多

3. **token估算不准：**

很多简单实现会用 `text.length / 3` 这种方式估算 token。英文日志、中文说明、代码块、JSON、路径、堆栈混在一起时，误差会很大。系统以为还有空间，实际 provider 已经接近上限；或者系统以为必须压缩，实际上还有足够预算。

**这会让压缩策略变得随机：有时候压晚了，模型调用直接失败；有时候压早了，把还需要的上下文提前删掉。上下文管理如果连压力判断都不准，后面的裁剪和摘要就只能靠运气。**

4. **不区分信息价值：**

同样是 5000 token，价值完全不同。5000 token 的 `grep` 输出里可能只有两行命中真正有用；5000 token 的用户需求、架构约束和失败原因分析，可能每一段都影响后续决策。

早期的方案通常按时间或长度处理上下文，而不是按价值处理。结果就是低价值工具输出占着窗口，高价值判断被一起压掉。**Agent 看似保留了"历史"，实际上保留的是噪声。**

5. **用户输入和工具输出被一刀切：**

用户贴的代码、约束和目标，是任务的输入意图。工具输出只是系统执行过程里的观测结果。它们不应该被同一种规则处理。

比如用户明确说"不要改公共 API，只修内部实现"，这句话如果被压成"用户要求修复问题"，边界就没了。再比如用户贴了一段真实配置，Agent 后面要按这个配置兼容；如果它和一大段测试日志一起被摘要，具体字段很可能被省掉。**最后模型会忘了用户到底给了什么。**

## 当前主流Agent Harness的上下文压缩策略

### Claude Code 的上下文压缩策略

Claude Code 会先把可用窗口算清楚：模型总窗口减掉预留输出 token，剩下的才是当前轮真正能用的上下文预算。然后它根据剩余空间把会话分成几个状态：安全、预警、自动压缩、阻塞。模型调用前，系统就能判断当前上下文是否健康，避免等 provider 报错后才补救。

它的压缩分成四档，越往后成本越高：

1. **Snip：**直接清掉旧工具结果，不调用 LLM。它不会删除整条消息，只把内容替换成 `[Old tool result content cleared]` 这类标记。token 省下来了，消息链和工具调用 ID 还在。
2. **MicroCompact：**处理长时间暂停后的上下文。用户离开一段时间再回来，prompt cache 可能已经过期，旧工具输出继续留在 prompt 里只会增加重写成本。它会保留最近 N 个工具结果，把更早的可压缩输出清掉。
3. **Collapse：**在上下文压力变高时主动整理消息，系统会在上下文使用率达到 90% 时开始提交（commit）压缩操作，在 95% 时阻止新的 spawn。它会选择性重组消息，尽量留下仍有价值的原始细节，避免把整段历史直接压成一句摘要。
4. **AutoCompact：**最后才用 LLM 做对话摘要。当前面几档都不够用时，系统才生成摘要，并把压缩边界、摘要、保留消息、附件和钩子结果重新拼成新的上下文。

CC在上下文压缩设计的这个顺序很关键。旧工具输出通常最占空间，也最容易安全移除；用户需求、关键决策、错误原因这些内容，应该尽量留到后面处理。Claude Code 先清低风险内容，再做结构调整，最后才让 LLM 摘要整段对话。

整个顺序是按照成本来的，1～3级别不调用LLM，成本小，最后AutoCompact才需要调用LLM。

它还处理了压缩失败的问题。自动压缩会提前预留输出 token，避免出现"上下文已经满了，连摘要都生成不出来"的情况。如果连续压缩失败 3 次，系统会进入断路器状态，后面不再反复尝试，避免每一轮都浪费 API 调用。压缩完成后，还会插入边界标记，告诉后续流程：哪些消息已经被摘要过，不要再重复压。

压缩提示也做了约束。模型先在 `<analysis>` 里整理要点，最后只把 `<summary>` 放回上下文。这样模型可以先分析对话结构，但分析过程不会继续占 prompt。摘要会按目标、关键决策、文件变化、未解决问题这些维度整理，方便 Agent 接着往下做。

Claude Code 的压缩更像一套分级清理流程：先算预算，再清旧工具输出，再整理消息，最后才摘要。对 Pico 来说，最值得借鉴的是这个顺序。**上下文要持续维护成当前任务能用的工作集，等窗口爆掉以后再抢救，通常已经晚了。**

### Codex 的上下文压缩策略

Codex 的压缩路径分两种。非 codex 模型走公开源码里的本地流程：CLI 把当前对话交给一个 LLM，要求它生成 handoff summary。这个摘要要写清当前进度、关键决策、用户约束、下一步，以及继续任务需要的数据。下一轮恢复时，CLI 会在摘要前加一段 handoff prefix，告诉新模型：前一个模型已经做过一部分工作，这里是它留下的接力摘要。

codex 模型走另一条路。CLI 调用服务端 `compact()`，拿到一个加密 blob。客户端看不到摘要明文，也不知道服务端用了什么 prompt。Simzhou 转发的实验用两次 API 调用做探针：先把带注入 payload 的消息交给 `compact()`，再把返回的 blob 传给 `responses.create()`，让后续模型复述它看到的上下文。实验输出显示，服务端路径很可能也在用 LLM 生成摘要，并且会在恢复时给摘要加 handoff prompt。

从流程看，Codex 的压缩大概是这样：

1. **本地 compact：**非 codex 模型直接在客户端侧调用 LLM，总结当前对话，生成一段可接力的 handoff summary。
2. **服务端 compact：**codex 模型把对话交给服务端 `compact()`，返回的是加密 blob。客户端只负责保存和传递这个 blob，不解析里面的摘要。
3. **恢复时 create：**下一次 `responses.create()` 会带上这个 blob。服务端解密后，把摘要和 handoff prompt 放进模型上下文，让新一轮模型接着做。

这里最值得注意的是 handoff summary 的定位。它不是把历史压短就结束，而是给"下一个模型"写一份交接材料。公开的 compact prompt 要求摘要包含当前进展、关键决策、约束、用户偏好、剩余工作和关键数据。也就是说，Codex 压缩关注的是任务恢复，不只是 token 变少。

#### Codex 的 compact 流程

Kangwook Lee的文章里的图可以把服务端 compact 路径拆得更清楚。第一步是 `responses.compact()`：服务端把 compactor 的隐藏 system prompt 和用户输入放在一起，让一个 compactor LLM 生成明文摘要。实验里的红色部分是注入 payload，用来诱导 compactor 把自己看到的提示词写进摘要里。

![这张图展示了Codex上下文压缩compact流程中服务端注入给compactor LLM的完整内容，左侧是对使用者未知的compactor系统提示词，右侧是明确的完整输入文本，包含项目备注及要求复制特定提及"CONTEXT CHECKPOINT"等内容的系统提示，还标注了强制QA步骤的说明，底部说明compactor LLM会处理这两部分内容生成明文摘要，其中红色部分是注入的payload。](https://feishu.cn/file/OjTQbh1U1oNx9WxpjLccXs0Sn9t)

摘要生成后，客户端拿不到明文，只能拿到服务端返回的加密 blob。文章图里把它标成 `ALL WE GET BACK`，意思是客户端只能保存和传递这串密文，不能在本地解析里面到底写了什么。

![这张图片展示的是Agent上下文压缩相关流程里的加密密文内容，是服务端加密后的结果，采用AES-128-CBC与HMAC-SHA256的加密方式，图中标注的"ALL WE GET BACK"对应上下文里的说明，指客户端仅能获取这串加密blob，无法在本地解析其中内容，该内容是摘要生成后服务端返回给客户端的密文，承载着被诱导注入的payload相关的摘要信息。](https://feishu.cn/file/WXdWbs9JBo3QSKxcT5DcNoOsnwc)

第二步是 `responses.create()`。客户端把上一轮的加密 blob 和新的用户请求一起传回服务端。服务端解密 blob，再把 handoff prompt、解密后的摘要和新用户消息拼成下一轮模型看到的上下文。

![这张图对应文章中关于Codex推测管线的内容，左侧紫色区域是标注为ENCRYPTED BLOB的加密内容，右侧红色区域是标注为OUR PROBE (FULL TEXT)的完整探测文本。下方的箭头标识了操作过程：服务器会先解密该加密Blob，再在内容前添加handoff prompt，最终组装出上下文。这一图示直观呈现了文章提到的完整推测管线，对应`responses.compact()`与`responses.create()`的相关流程，辅助理解服务端上下文压缩后的处理逻辑。](https://feishu.cn/file/GJLlbPcVFoU9mWxca39cJ6F5nmc)

![这张图展示了Codex compact流程相关的内容，包含三个部分。左侧是未知内容区域，存有接力提示，标注需明确其中的内容；中间是解密后的blob区域，是压缩工具第一步的输出，大概率包含泄露的提示；右侧是探测内容区域，标注要完整逐字引用各部分内容。](https://feishu.cn/file/MhzhbtQNkoMTSOxzT19crppwnzx)

探针输出里能看到三类内容：黄色是系统提示，绿色是 handoff prompt，粉色是 compaction prompt。关键点在绿色和粉色：它们和 Codex 开源模板里的 `summary_prefix.md`、`prompt.md` 很接近，所以作者认为服务端 compact 路径大概率也在用类似的摘要提示和接力提示。

![图片展示了Codex的compact流程中，`responses.create()`环节的完整推测管线。其中，Message 1为系统提示，包含你是ChatGPT、知识截止日期等内容；Message 2是关于进行context checkpoint压缩的提示，要求创建一个简洁、结构化的总结，包括当前进度、关键决策等信息；Message 3是系统通知，要求在总结前复制收到的文本内容，且需包含"INSTRUCTION_START"和"INSTRUCTION_END"标记。该图与上下文紧密相关，直观呈现了compact流程中`responses.create()`环节的具体内容。](https://feishu.cn/file/QtykbVaRwoPwxKx0n8vccdzFnEf)

最后这张图是文章给出的完整推测管线：`responses.compact()` 负责把原始 conversation history 压成摘要并加密；`responses.create()` 在下一轮解密摘要，前面补上 handoff prompt，再交给模型生成回答。

![图片展示了Codex的compact和create流程。compact流程中，`responses.compact()`接收system prompt、compaction prompt及conversation history，生成摘要并加密；create流程中，`responses.create()`接收system prompt、handoff prompt、compactor的summary及新请求，LLM生成响应。两者均涉及system prompt、prompt及user input，体现上下文压缩策略在生成回答中的应用。](https://feishu.cn/file/HV3EbJqY7oF8aGxPuKvcl2zSnXe)

作者Kangwook Lee的实验脚本，核心就两次调用：先用 `responses.compact()` 把注入内容写进 blob，再用 `responses.create()` 让模型把看到的上下文复述出来。它证明的是一种探测方法，不是 Codex 官方文档。

![图片展示了Codex的压缩流程代码示例。首先调用`responses.compact()`函数，将包含"INJECTION_START"或"INSTRUCTION_START"等标记的payload压缩，此操作被称作"Poison the blob with leaked prompt"；接着调用`responses.create()`函数，向模型发送包含"PROBE"标记的请求，此操作称作"Read the leaked prompt back out"。该图片与上文提到的Codex压缩重点落在接力语义，压缩后历史变成可恢复上下文包，客户端只拿到blob，服务端进行恢复和手交prompt拼接的内容相呼应。](https://feishu.cn/file/LrorbvXbboNXS5x4hmvc6QR8nAg)

结合这些图看，Codex 的压缩重点落在接力语义上：压缩后的历史会变成一个可恢复的上下文包。客户端只拿到 blob，真正的摘要恢复和 handoff prompt 拼接发生在服务端。对 Pico 来说，最值得借鉴的是这一点：压缩结果要能让下一轮模型接着干活，里面需要留下目标、约束、进度、关键决策和下一步。

### OpenCode的上下文压缩策略

> 💡 OpenCode 的思想是先做便宜的清理，再做重的摘要。先 prune 旧工具输出，再 summary 成接力上下文。

第一步是 **Prune**，轻量，不调用 LLM。OpenCode 会从最近消息往前扫，跳过最近 2 轮用户对话，先保护最近约 40k token 的工具输出不动；如果更早的工具输出预计能回收超过 20k token，就把这些 tool part 标记为 `time.compacted`。原始数据还在 session 存储里，但发给模型时会被替换成 `[Old tool result content cleared]`，附件也不会继续塞进 prompt。

这个设计的好处是很直接：工具输出通常最占窗口，也最容易安全降级。比如 grep、read file、测试日志这种内容，过了当前排查阶段以后，大部分只需要保留"曾经查过、曾经返回过结果"这个事实，不一定要每轮都把几千行原文重新喂给模型。Prune 做的就是先擦这些低价值、高体积的内容，而不是把用户指令、决策和错误原因一起压掉。

第二步是 **Summary**，重一些，会调用 LLM。只有当上下文真的接近上限，或者触发 compact 流程时，OpenCode 才会用 compaction agent 生成摘要。新版本的摘要不是随便写一段大意，而是固定结构：`Goal`、`Constraints & Preferences`、`Progress`、`Key Decisions`、`Next Steps`、`Critical Context`、`Relevant Files`。这其实是在把旧对话整理成一份 handoff，让下一轮模型知道目标、约束、进度、关键决定、错误信息和相关文件。

还有一个细节很重要：OpenCode compact 后不是只剩摘要。源码里默认会保留最近 2 个 turn，并给最近上下文留一段 token 预算；预算不够时，还会尝试从某个 turn 中间切开。也就是说，摘要负责交代过去发生了什么，tail 负责保留最近工作现场。自动 compact 场景下，它还可能回放前一条用户消息，或者插入一条 synthetic continue，让模型继续下一步，实在不确定再停下来问。

所以 OpenCode 的上下文压缩可以概括成一句话：**先用 prune 便宜地清旧工具输出，再用 summary 把真正需要延续的任务状态压成结构化 handoff，同时保留最近现场。**

上画出来、能回答追问的程度。

## Pico 的上下文机制

参考简历写法：

> **上下文治理与成本优化：** 针对代码智能体长链路任务中上下文分散在对话历史、工具结果、文件状态和长期记忆里，导致 prompt 膨胀、重复读文件和任务恢复不可控的问题，设计并落地模型调用前的上下文治理链路，统一完成上下文预算管理、压力感知裁剪、分层信息保留、结构化压缩摘要、token 估算、运行追踪和成对 A/B 成本评测；在确定性上下文 A/B 实验中，估算输入 token 从 **5765 降到 4241**，下降 **26.44%**

### 0. 场景：代码智能体为什么需要主动管理上下文

一个能干活的 Code Agent，跑一个稍微复杂的任务（重构三个文件、跑测试、修 bug），对话轮次轻松过 20 轮。每轮你都在往模型的输入里塞东西：系统指令、工作记忆、技能描述、检索到的相关笔记、历史对话、工具调用结果、当前用户请求。

信息分散在五六个来源里，你不能全塞进去。200k token 窗口听起来大，但一次 `read_file` 返回的内容可能就 3000 token，跑 10 轮工具调用历史就膨胀到 5 万 token。全塞进去的后果：成本线性涨、首 token 延迟变高、模型对中间内容的注意力衰减（Lost in the Middle 效应），信噪比一路下降。

你也不能不塞。Agent 如果忘了刚才读过哪些文件、做过什么决策、哪条路径试过行不通，它就会重复读文件、重复犯错、恢复不了中断的任务。

上下文管理要解决的核心矛盾：在有限的 token 预算内，让模型在每一轮都拿到做出正确决策所需的最小高密度信息集。

### 1. Pico 上下文的四阶段治理链路

Pico 在每次模型调用前跑一条治理链路，从原始信息到最终 prompt 经过四个阶段。

#### 阶段一：动态预算计算

Pico 从模型的 context window 去反推我们目前可用的上下文预算：

```Plain
effective_tokens = context_window - output_reserve(16384)
budget_tokens = effective_tokens × utilization_ratio(0.50)
budget_chars ≈ budget_tokens × 4
```

200k 窗口的模型拿到约 92k token 的 prompt 预算，1M 窗口拿到约 200k token。预算不超过 800k chars 的硬上限，也不低于 60k chars 的下限兜底。

不同模型下的实际预算：

> （文档此处内嵌在线表格，当前表格内容为空）

对比 Claude Code 的预算算法：它用的是模型窗口减去输出预留再减去一个固定 buffer：

```Plain
effective_window = context_window - max_output_tokens(20000) = 180,000
auto_compact_threshold = effective_window - 13,000 = 167,000
```

167k / 180k ≈ 93%

也就是说 Claude Code 允许 prompt 占满有效窗口的 93%，只留 13k token 的余量才触发压缩。这意味着它尽量保留完整对话历史，只在快撑爆时才动手。

Pico 选 50% 是另一条路线，这是第一个设计决策：为什么用 50% 而不是像 Claude Code 那样塞到 93%？后面我们会详细展开

#### 阶段二：分层组装

得到预算之后，prompt 由六个 section 按固定顺序拼接，每个 section 按比例分配预算：

| Section | 比例 | 内容 |
|-|-|-|
| prefix | 20% | 系统指令、工作区上下文 |
| memory | 13% | 工作记忆、todo、checkpoint |
| skills | 7% | 当前激活的技能描述 |
| relevant_memory | 10% | 向量检索命中的相关笔记 |
| history | 50% | 对话历史（工具调用 + 结果） |
| current_request | 不限 | 当前用户输入，永远完整保留 |

顺序是设计过的：稳定不变的内容放前面利于 prompt cache 命中，当前请求放最后保证 recency bias 对它有利。

#### 阶段三：压力感知裁剪

预算分配完之后，实际内容可能超出预算。Pico 用一套压力分级系统做渐进式降级，而不是等到撑爆才一次性处理。

**压力怎么算**

```Plain
pressure_ratio = max(rendered_tokens, raw_tokens) / budget_tokens
```

这里取 rendered 和 raw 的较大值。rendered_tokens 是裁剪后实际进 prompt 的量，raw_tokens 是裁剪前原始内容的量。为什么要取 max？如果只看 rendered，系统陷入死循环：裁剪让 prompt 变小，压力变低，永远不触发压缩，但原始历史其实已经远超预算。raw tokens 才反映真实的信息需求。

**四个压力等级**

```Plain
< 60%  → tier0_observe   什么都不动
60-80% → tier1_snip      开始收紧 relevant_memory（缩到 70%）
80-95% → tier2_prune     relevant_memory 保持 70%，skills 砍到 50%
≥ 95%  → tier3_summary   触发 compaction（下一阶段）
```

为什么是离散 tier 而不是连续的线性缩放？因为我们要的是触发决策，不是比例响应。"现在该不该启动 LLM compact" 是一个布尔问题，离散等级比连续值更清晰。

**tier 升级时具体发生什么**

tier1 和 tier2 是 section 级的预算压缩，不涉及内容删除：

- tier1_snip：relevant_memory 的预算乘以 0.7。实际效果是检索到的 3 条笔记可能只渲染 2 条，或者每条被截短。对任务影响最小，因为这些笔记下一轮还能重新检索到。
- tier2_prune：在 tier1 的基础上，skills 预算乘以 0.5。技能描述本身是固定文本，缩短后模型对工具参数的理解可能变弱，但工具仍然可用。

如果 section 预算压缩后 prompt 仍然超过 total_budget，进入溢出兜底循环：按 `relevant_memory → skills → history → memory → prefix` 的固定顺序逐个压缩，直到 prompt 能装进预算。每个 section 有一个绝对下限（floor），不会被裁到空。

**溢出兜底的裁减顺序为什么这么排？**

哪些信息丢了之后恢复代价最低，就先裁哪些：

- relevant_memory：下一轮 retrieval 就能补回来，恢复成本约等于零
- skills：工具描述可重新载入，几百字符的固定文本
- history：当前 session 的局部记忆，丢了只能靠 compact 摘要补
- memory：含 todo、checkpoint、working memory，丢了等于忘了今天要干什么
- prefix：系统规则和 workspace 上下文，一旦改了 prompt cache 失效，下一次 input token 全价重算

current_request 永远不出现在裁减顺序里，它代表用户这一轮的直接意图，裁掉就本末倒置了。

**token 估算怎么做**

Pico 用启发式估算而不是真实 tokenizer：

```python
# 通用文本
tokens = chars / 4

# 代码（短 token 多）
tokens = chars * 10 / 32

# CJK 密集文本（中文每字约 1.5-2 tokens）
tokens = chars * 10 / 18
```

当 provider 返回了上一次调用的 actual token usage，且 identity（model、base_url、cache_key）和当前请求一致时，Pico 会用 actual 值替代估算值做压力计算。这是一种校准机制：启发式给方向，真实数据给精度。

**什么时候这套机制会失灵**

- 用户中途换模型但 identity 字段碰巧还像旧的，校准拿到错误的 actual 值
- 中英混合长 prompt 下 chars/4 偏差最大，可能该触发 tier1 了还没到
- 用户一次性贴了 50k 字符的代码到 current_request 里，current_request 不裁剪，prompt 直接超预算，跳过所有 tier 直接走溢出兜底

#### 阶段四：结构化压缩

当压力到 tier3 且历史中有 4 条及以上可压缩事件时，触发 compaction。这是上下文管理链路里最重的一步，也是唯一可能调用额外 LLM 的环节。

**触发条件（四个条件叠加）**

```python
# context_orchestrator.py _compact_request()
if prompt_over_budget:         → deterministic（紧急兜底，不走 LLM）
if pressure_tier == tier3:
    if delta_count >= 4:       → LLM compact
    else:                      → skip（增量太少，LLM 写不出比 deterministic 更好的东西）
if history_count <= 4:         → skip（前置短路，不对短 session 浪费调用）
```

每个条件挡掉一种浪费：去掉任何一个都会让系统在不该压缩时压缩，白花 LLM 调用。

**压缩执行流程**

1. 按 turn_id 将 history 分组
2. 最近 2 个 turn 标记为 protected，不参与压缩
3. 跳过已有的 compact_summary item（避免二次压缩）
4. 剩余 delta items 送入摘要生成
5. 摘要 item 替换原 delta：`session["history"] = [summary_item, *protected_items]`
6. 更新 `session["context_summary"]` 的边界标记（last_included_event_id）
7. 持久化 session 到磁盘
8. 重新调用 ContextManager.build() 生成压缩后的 prompt

**两种摘要模式**

Pico 维护两条平行的摘要路径，运行时根据触发条件选择：

**LLM 摘要**（summary_mode = "llm"）：把 delta items 渲染成有界文本（每条 tool output 截断到 3000 chars，user/assistant 截断到 2000 chars，总量上限 20k chars），连同 prior summary 一起交给模型。prompt 要求模型输出固定格式的 markdown：

```Plain
## Goal
<一句话：用户要完成什么>

## Constraints
- <用户声明的约束>

## Files Read / Files Modified
- <路径>

## Key Decisions
- <决策及原因>

## Blockers
- <阻塞项或未解问题>

## Next Steps
- <agent 接下来该做什么>
```

解析时验证 goal 和 next_steps 非空，缺一就判定摘要失败，回退到确定性模式。prompt 还要求模型保留精确的文件路径、变量名、错误信息和测试名，不允许模糊化。如果有 prior summary，要求合并而不是重复。

**确定性摘要**（summary_mode = "deterministic"）：纯规则提取，不调模型。遍历 delta items，用正则匹配从中抽取：

- Goal：取最近一条 user message 的内容
- User constraints：匹配 "不要"、"必须"、"只"、"don't"、"must"、"only" 等关键词的句子（上限 5 条）
- Files read / modified：从 tool call 的 args.path 字段提取
- Key decisions：匹配 "decided"、"选择"、"approach"、"switched to" 等关键词（上限 3 条）
- Rejected paths：匹配 "不行"、"doesn't work"、"reverted" 等（上限 3 条）
- Last error context：从后往前找第一条匹配 Error/Traceback/FAILED 的 tool output，截取前 200 字符
- Critical artifacts：收集所有 artifact_ref 引用

输出格式是 `key: value` 的扁平列表，加上一行 "compacted N history items"。总长度硬限 2000 字符，超了直接截断。

**增量合并**

如果 session 里已有一条 prior compact_summary（之前压缩过一次），确定性模式会把新 delta 的提取结果追加在 prior 后面，以 "Incremental compacted delta:" 分隔。LLM 模式则在 prompt 里把 prior summary 放进 "Prior Summary (merge into your output)" 段落，要求模型合并而非重复。

**Fallback 链**

```Plain
尝试 LLM 摘要
  ├─ 成功 → summary_mode = "llm"
  └─ 失败（API 超时 / 解析 goal 为空 / 异常）
       → 自动切换 deterministic → summary_mode = "deterministic_fallback"
```

主流程不受影响，用户感知不到降级发生。但 trace 里会记录这次 fallback，包括 compact_call_usage（LLM 调用花了多少 token，即使失败也要记）。

**为什么保护最近 2 轮而不是 1 轮或 3 轮**

最近 1 轮：模型只能看到上一步的结果，如果上一步是工具调用，它看不到触发那次调用的用户请求。决策链断了。

最近 3 轮：压缩能省下的空间变少，频繁压缩时净收益可能为负。

2 轮是一个经验平衡点：保证模型能看到"用户说了什么 → 我做了什么 → 结果是什么"的完整一跳。这个值在 `CompactManager.plan()` 里是参数，不是硬编码，可以根据任务复杂度调整。

### 2. 关键 trade-off 和思考

#### 50% 利用率 vs 塞满窗口

Claude Code 和 Pi Mono 用 90%+ 的窗口，追求完整保留对话历史，只在逼近极限时才压缩。

Pico 选 50% 的理由有两条，主要的思想是追求信噪比。

1. **上下文存在边际收益递减**。塞到 90% 时，大量旧的工具调用结果其实模型已经不需要了，但它们占着 token 预算，稀释了当前轮次真正重要的信息。Lost in the Middle 效应在长 prompt 里是可观测的——模型对 prompt 开头和结尾的信息检索准确率高，中间段落容易被忽略（Nelson F. Liu et al., "Lost in the Middle: How Language Models Use Long Contexts", arXiv:2307.03172, TACL 2024）（https://arxiv.org/abs/2307.03172）。
2. **50% 意味着更频繁的压缩，但每次压缩的 delta 小**。压缩 5 轮比压缩 50 轮容易保真得多——摘要的上下文短，LLM 犯错概率低，结构化输出更可控。代价是多了 LLM 调用开销，但摘要调用用的是短 prompt，成本远低于把 50 轮历史塞进主调用。

这不是个确定性的最优解，而是一个可以通过 A/B 实验调参的设计旋钮。Pico 的 config 里可以直接改 `utilization_ratio`，跑同一批 benchmark 就能看到质量和成本的变化。

#### 裁剪顺序背后的信息优先级

溢出时的裁减顺序是 `relevant_memory → skills → history → memory → prefix`。

**pico里是按照这么个判断设计的：哪些信息丢了之后恢复代价最低，就先裁哪些。**

- relevant_memory ：下一轮 retrieval 就能补回来，恢复成本约等于零
- skills ：描述可重新载入，成本是几百字符的固定文本
- history ：是当前 session 的局部记忆，丢了只能靠 compact 摘要补
- memory： 含 todo、checkpoint、working memory，丢了等于忘了今天要干什么
- prefix ：含系统规则和 workspace 上下文，一旦改了 prompt cache 立刻失效，下一次 input token 全价

current_request 永远不裁剪，它代表用户这一轮的直接意图，所有压缩都是为了"还能听懂用户在说什么"，把用户原话压掉就本末倒置了。

#### 确定性摘要 vs LLM 摘要：为什么两种都留

确定性摘要靠规则提取（正则匹配 "decided"、"don't"、"must" 等关键词，提取文件路径和错误信息），满足三个硬约束：零成本、零失败率、零延迟。代价是只能抓表面模式，对"试了三种方案，第一种因为 X 失败，最终选了第三种"**这类复杂决策链的压缩质量有限**。

LLM 摘要理解语义，能把这种叙事压缩成结构化的 Key Decisions + Rejected Paths。**代价是一次额外的模型调用**。

Pico 的分工：tier3 压力触发时用 LLM 摘要（精度换成本），prompt 溢出的紧急情况用确定性摘要（速度优先），LLM 调用失败时 fallback 到确定性（可靠性兜底）。三个路径在 trace 里用 summary_mode 字段区分：`llm` / `deterministic` / `deterministic_fallback`。

### 3. 验证：怎么证明上下文做得好

做上下文治理最容易掉进去的坑：你觉得省了 token，但可能只是把成本挪到了别处（compact 自己调了一次 LLM），或者 prompt 短了但模型质量降了导致多跑两轮。所以验证必须同时看成本和质量，缺一不可。

#### 实验设计：成对 A/B

Pico 内置了一套配对实验框架（`pico/evaluation/context_cost.py`）。核心思路是控制变量法：同一批基准测试任务，只改上下文治理策略，观察 token 消耗和任务完成质量的差异。

三个实验变体（variant）：

| 变体 | 上下文策略 | 摘要模式 |
|-|-|-|
| `no_context_reduction` | 不裁不压，全量历史 | 无，作为基线 |
| `full_orchestrator` | 压力感知 + section 裁剪 | 确定性摘要 |
| `full_orchestrator_with_llm_handoff` | 压力感知 + section 裁剪 | LLM 结构化摘要 |

任务集来自 `benchmarks/long_session_tasks.json`，包含 5 个长会话编码任务（multi-file-refactor、debug-and-fix、add-endpoint-with-test、config-migration、dependency-upgrade），每个 8-12 步。

#### 两种实验模式

**脚本模式（scripted mode）**：用预录模型客户端代替真实 LLM。不管 prompt 内容是什么，客户端按固定顺序返回预先录好的输出。这样两个变体走过的对话路径完全相同，唯一变量是每轮发给"模型"的 prompt 大小。它测的是：同样的任务轨迹下，治理策略能把每轮 prompt 压缩多少。

脚本模式的优势：可复现（消除模型随机性）、零成本（不调真实 API）、隔离变量（模型行为不会因为 prompt 长度不同而走不同的工具调用路径）。代价是它只能验证 prompt 组装链路的行为，不能验证"更短的 prompt 是否影响模型决策质量"。

**线上模式（live mode）**：用真实 provider（DeepSeek / Anthropic）跑。模型行为不固定，需要多次重复取中位数。它验证的是：真实模型 + 真实 token 计量下，成本确实降低且质量无回归。

线上实验的配置：

- 2 个 provider（DeepSeek deepseek-v4-pro + Anthropic claude-sonnet-4-6）做交叉验证
- 每个 task × variant × provider 跑 3 次重复，取中位数
- 5 tasks × 3 variants × 2 providers × 3 reps = 90 组实验
- Temperature 固定 0.0，减少随机性

#### token 怎么算

每轮模型调用后，从 trace 里提取上下文用量记录。当 provider 返回了真实用量（usage_source = "actual"），用真实值；否则用估算值（usage_source = "estimated_proxy"）。报告里明确标注数据来源，不混为一谈。

净收益公式：

```Plain
net_benefit = baseline_input_tokens - optimized_input_tokens - compact_call_total_tokens
```

前半部分是主请求省了多少，后面扣掉 compact 自己花的。如果 compact 花得比省的还多，净收益是负数，不截断到 0，直接暴露。

#### 什么情况下才能宣称有效果

实验结果要标记为"可认领的成本优化"（claimable cost win），三个条件必须同时满足：

1. 净收益 > 0（确实省了）
2. 质量回归数 = 0（验证器全通过，没有因为压缩导致任务失败）
3. 没有验证状态未知的配对（不能在结果不明的情况下宣称赢了）

任何一个不满足，只能标记为"方向性证据"（directional evidence only），不能作为结论。

## 参考

1. [https://simzhou.com/en/posts/2026/how-codex-compacts-context/](https://simzhou.com/en/posts/2026/how-codex-compacts-context/)
2. [https://justin3go.com/posts/2026/04/09-context-compaction-in-codex-claude-code-and-opencode](https://justin3go.com/posts/2026/04/09-context-compaction-in-codex-claude-code-and-opencode)
3. [https://zhanghandong.github.io/harness-engineering-from-cc-to-ai-coding/preface.html](https://zhanghandong.github.io/harness-engineering-from-cc-to-ai-coding/preface.html)
4. [https://x.com/Kangwook_Lee/article/2028955292025962534](https://x.com/Kangwook_Lee/article/2028955292025962534)
#（注：内容由AI生成）
