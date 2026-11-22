# 字节日常Agent一面面经-生菜同学

# 0. 考察内容

原始问题是这一组：

1. 负责的部分，具体讲解上下文管理、裁剪顺序
2. 项目如何保证任务不跑偏
3. 介绍一下 Transformer 架构
4. 项目使用了哪些库，用的是什么框架，是什么 Agent 范式
5. skills 执行时产生的上下文是如何处理
6. 讲解一下函数调用是什么
7. 工具如果是合理的重复调用，系统会怎么处理
8. 模型输出不稳定怎么处理
9. 模型参数给错怎么处理
10. Freshness 校验是什么意思

追问路径很清楚：

- 先问你负责什么，其实是在看你能不能把项目讲成一个系统
- 再问上下文和跑偏，其实是在问长任务里状态怎么管
- 再问工具调用、重复调用、参数错误，其实是在问 runtime 有没有边界
- 最后问 freshness，其实是在问恢复时旧信息还能不能信

> 💡 面试时候建议回答的方向：每题不只给正确答案，还要讲为什么这么设计、其他方案为什么不够、Pico 现在选择的 tradeoff 是什么。面试官听到 tradeoff，才会觉得你不是背项目，而是真的参与过设计。

# 1. 开场怎么讲 Pico

要先把项目定位讲清楚：

Pico 是一个运行在本地代码仓库里的轻量 code agent harness。它不是单纯把用户问题发给模型，而是把模型放进一个受控的工程运行时里：每一轮先组装仓库上下文、工作记忆、历史记录和当前请求，再让模型输出工具调用或最终答案。模型不能直接碰文件系统，只能通过 Pico 暴露的白名单工具申请读文件、搜索、运行命令、写文件或者打补丁。

这个项目要解决的不是"模型会不会写代码"，而是 code agent 落地以后必然遇到的几个工程问题：

- 上下文会膨胀，旧信息可能污染当前任务
- 工具调用会失败、重复、越界，不能靠模型自觉
- 任务中途会停，需要 checkpoint 和恢复状态
- 文件摘要可能过期，不能盲信 memory
- 模型说完成不等于任务真的完成，需要 verifier 和 benchmark

所以 Pico 的核心价值在 harness 层：上下文管理、工具治理、任务状态、运行审计、恢复机制和评测闭环。

# 2. 负责的部分，具体讲上下文管理和裁剪顺序

**多轮 code agent 的上下文膨胀，不只是 token 问题，而是状态管理问题。**

因为 prompt 里混进来的东西有很多种：系统规则、工具说明、仓库快照、当前任务、旧工具输出、文件摘要、历史对话、checkpoint、相关记忆。它们的生命周期不一样，可信度不一样，重要性也不一样。如果都混在一起压缩，模型很容易抓错重点。

Pico 的设计不是把所有历史压成一个 summary，而是先分层，再预算。

每轮发模型前，`ContextManager` 会按固定顺序组装：

1. `prefix`：稳定信息，比如系统规则、工具说明、仓库快照
2. `memory`：工作记忆，比如当前任务摘要、最近文件、文件摘要
3. `relevant_memory`：和当前请求相关的少量记忆
4. `history`：近期对话和工具结果
5. `current_request`：当前用户请求

这里最重要的设计是：**current request 永远不裁剪。**

因为当前请求是本轮任务的真实目标。如果为了省 token 把当前请求裁坏，模型后面跑得再努力也是错方向。

当前 `main` 分支默认总预算是 `12000` 字符级预算。各段也有自己的预算：

- `prefix`：3600
- `memory`：1600
- `relevant_memory`：1200
- `history`：5200

超预算时按这个顺序压缩：

1. 先压 `relevant_memory`
2. 再压 `history`
3. 再压 `memory`
4. 最后才压 `prefix`

这个顺序背后的 tradeoff 是：

> 💡 `relevant_memory` 可能有帮助，但它不是当前任务事实本身，所以先牺牲。`history` 很重要，但旧历史可以摘要化。`memory` 是工作集，不能太早丢。`prefix` 里有工具协议和系统规则，如果太早裁掉，模型可能连工具怎么调用都忘了。当前请求则完全不裁，因为目标不能丢。

如果面试官追问"那历史具体怎么压"，要说得更细：

多轮历史真正进入 prompt 前，主要由 `ContextManager` 做压缩。最近几条尽量保留，旧工具输出会压成摘要；旧 `read_file` 如果有新鲜的 `file_summaries`，会渲染成 `path -> summary`；同一路径的旧重复读取也会折叠。`history_text()` 更多用于统计和委派摘要，不能把摘要复用全部归到它。

这里也有一个取舍：

- 只做滑动窗口最简单，但容易忘早期约束
- 只做大摘要成本低，但容易摘要漂移
- 只做检索适合静态知识，不适合动态任务状态
- 结构化状态更稳，但工程复杂度更高

所以 Pico 当前选择的是一个折中方案：稳定规则进 prefix，短期任务信息进 memory，相关旧事实走 relevant memory，完整过程落 trace，当前 prompt 只带最小可用工作集。

对应代码：

- `pico/context_manager.py`：section 顺序、预算、裁剪顺序
- `pico/context_manager.py`：history section 压缩和 `file_summaries` 复用
- `tests/test_context_manager.py`：当前请求保留、旧工具输出摘要化、重复 read 折叠

> 💡 **v3 版本可以补充：**skills 已经不是塞在 prefix 里的普通说明，而是 `ContextManager` 里的独立 section。

如果按 v3 版本回答，prompt 顺序可以讲成：

```text
prefix -> memory -> skills -> relevant_memory -> history -> current_request
```

这里的设计变化是：skill catalog 被当成一种"可发现的工作流入口"，和工具协议、工作记忆、历史记录分开管理。prompt 里默认不会把每个 skill 的完整正文都塞进去，而是只渲染可用入口，例如：

```text
Available skills:
- /review: ...
- /test: ...
```

只有用户真正调用某个 skill 时，才展开该 skill 的正文。

v3 默认总预算是 `60000` 字符，其中 skills section 默认预算是 `4000` 字符；当前实现的默认 floor 按 section budget 的四分之一计算，所以 skills 默认 floor 是 `1000` 字符。超预算时按这个顺序压缩：

```text
relevant_memory -> skills -> history -> memory -> prefix
```

面试里可以这样解释 tradeoff：

- 如果把大量 skill 正文都放进 prompt，模型确实更容易"知道有这些能力"，但 prompt 会迅速膨胀，长上下文成本和噪声都会上来。
- 如果只在外部保留 skill，模型又不知道什么时候该用。
- v3 选择折中：prompt 里先放轻量 skill catalog，让模型知道有哪些入口；真正调用某个 skill 时，再把具体 skill prompt 展开进本轮请求。这可以理解成 progressive disclosure 的雏形。

对应代码：

- `pico/core/context_manager.py`：`skills` section、预算、压缩顺序
- `pico/features/skills.py`：`render_prompt_section()`
- `tests/test_context_manager.py`：section 顺序测试

# 3. 项目如何保证任务不跑偏

核心思想：

**Pico 不靠模型自觉保证不跑偏，而是把任务推进变成一套可控的 runtime 状态机。**

可以拆成五层：

第一层是目标保留。当前请求不裁剪，所以模型每轮都能看到本轮任务目标。

第二层是工具协议。模型不能自由行动，只能输出工具调用或最终答案。工具调用要经过 `run_tool()`，再检查工具是否存在、参数是否合法、路径是否越界、是否重复调用、是否需要审批。

第三层是任务状态。每次 `ask()` 会创建 `TaskState`，记录 `run_id`、`task_id`、`tool_steps`、`attempts`、`last_tool`、`stop_reason`、`final_answer`。所以系统知道自己是 running、completed、stopped 还是 failed。

第四层是运行审计。每轮 prompt 构建、模型请求、模型解析、工具执行、checkpoint 创建、run 结束都会写进 `.pico/runs/<run_id>/`。出了问题可以回看 trace，而不是猜模型做了什么。

第五层是外部验证。Benchmark 里每个任务都有 fixture、allowed tools、step budget、expected artifact 和 verifier。通过不靠模型说"Done"，而是看 verifier 是否过、产物是否存在、是否在预算内、stop reason 是否正常。

> ✅ 让每一步都落在目标、工具、状态、trace、verifier 这五个约束里。

这里有一个关键 tradeoff：

如果约束太少，模型会更自由，短 demo 可能显得更聪明，但长任务容易失控。  
如果约束太多，系统会更安全，但模型可能显得保守，甚至因为审批和校验太多降低效率。  
Pico 当前倾向于后者：先把边界守住，再谈能力扩展。因为 code agent 写文件、跑命令、改补丁，是有副作用的，不能用聊天机器人的容错标准来设计。

对应代码：

- `pico/agent_loop.py`：主循环
- `pico/runtime.py`：工具执行总闸口
- `pico/task_state.py`：状态和 stop reason 分开记录
- `pico/run_store.py`：trace / report / task state
- `pico/evaluator.py`：外部 verifier 判定任务是否真的通过

# 4. 介绍一下 Transformer 架构

基础八股

Transformer 的核心是 self-attention。输入文本先被切成 token，经过 embedding 变成向量，再加入位置信息。每层主要有多头自注意力和前馈网络，中间有残差连接和 LayerNorm。

self-attention 会把每个 token 映射成 Query、Key、Value。Query 和 Key 做相似度计算，得到当前位置应该关注哪些 token，再对 Value 加权聚合。多头注意力相当于从多个子空间理解上下文关系。

现在常见大语言模型多是 decoder-only Transformer，本质上是在已有上下文基础上预测下一个 token。

# 5. 项目用了什么框架，是什么 Agent 范式

Pico 没有依赖重型 Agent 框架，主要是 Python 标准库加自己实现的轻量 agent loop。

模型后端统一抽象成：

```text
complete(prompt, max_new_tokens)
```

当前支持：

- Ollama
- OpenAI-compatible Responses API
- Anthropic-compatible Messages API
- DeepSeek 的 Anthropic 兼容接口

Agent 范式上，可以说它接近 ReAct：

```text
模型决策 -> 工具执行 -> 观察结果 -> 再决策 -> 最终答案
```

但 Pico 更强调 harness，而不是 ReAct 。因为真正影响 code agent 稳定性的，不是"我用了什么范式"，而是：

- prompt 怎么组
- 工具怎么定义
- 参数怎么校验
- 错误怎么反馈
- 状态怎么记录
- 任务怎么验证

这里的 tradeoff 是：

> 💡 用 LangChain / LangGraph 这类框架，能更快搭流程，也有现成生态；但缺点是很多 runtime 细节会被框架包起来，面试时很难讲清楚工具边界、上下文预算、状态恢复到底怎么做。Pico 选择自己写轻量 loop，是为了把 code agent 最关键的几条链路暴露出来，方便理解、评测和定制。

对应代码：

- `pico/models.py`
- `pico/agent_loop.py`
- `pico/runtime.py`
- `pico/tools.py`

# 6. skills 执行时产生的上下文如何处理

Pico 当前上下文主要来自 workspace prefix、memory、relevant memory、history 和 current request。skills 如果后续接入，不能变成无限追加 prompt 的东西，而应该被当成受控的 prompt 模块。

我会从四层设计：

第一，skill 的说明进入上下文预算。它不能绕过 `ContextManager`，应该作为 prefix 或 relevant context 的一部分，接受预算控制。

第二，skill 执行产生的工具结果短期进入 history。比如 skill 指导 agent 读了某个文件，完整工具结果先进入会话历史，保证可回放。

第三，只有高价值信息进入 memory。比如读过关键文件，可以沉淀文件摘要；临时日志、失败栈、过程性输出不应该长期保存。

第四，skill 不能绕过工具边界。skill 可以告诉模型"怎么做"，但真正执行仍然要走 `run_tool()`，继续受工具白名单、参数校验、路径边界和审批控制。

这里最重要的 tradeoff 是 skill 和 MCP 的分工：

- skill 更像 prompt / workflow，解决模型"该怎么做"
- MCP 更像 tool provider，解决模型"能调用什么"

如果把 skill 设计成能直接执行任意代码，那它会绕开 runtime 边界，风险很高。Pico 更适合先把内置工具抽象成 ToolProvider，再接 MCP；skill 则作为受预算、可召回的工作流说明进入上下文。

> 💡 面试时可以明确说：当前项目还没有完整 skill loader，这部分是后续设计。已经实现的是静态工具注册、上下文预算、history/memory/trace 这些底座。

> 💡 **v3 版本可以补充：**这里可以从"后续设计"补充到"当前代码已有一个 prompt-driven skill loader，但还不是完整插件生态"。

v3 里 skills 的实现可以分三层讲。

**第一层是发现和加载。**Pico 会先加载内置 skill，比如 `/review`、`/test`、`/commit`、`/simplify`，再加载用户目录和项目目录里的 skill：

- `~/.pico/skills/<name>/SKILL.md`
- `<repo>/skills/<name>/SKILL.md`
- `<repo>/.pico/skills/<name>/SKILL.md`

skill 文件本质是 Markdown + frontmatter。frontmatter 里可以写 `description`、`when-to-use`、`context`、`allowed-tools`、`paths`、`model`、`disable-model-invocation` 等 metadata。

**第二层是上下文注入。**正常 prompt 里只放可用 skill 列表，不放所有 skill 正文。调用 `/deploy staging` 这类 slash command 时，才会渲染成：

```text
Skill: deploy
Source: project
Context: inline
Arguments: staging

...skill 正文...
```

这能回答"大量工具或 skill 怎么办"的追问：不能直接全塞 prompt，而是先暴露轻量索引，再按需展开具体内容。

**第三层是执行边界。**skill 不是执行器，它只是 workflow prompt。真正读文件、搜索、写文件、跑命令，仍然必须走 Pico 原来的工具调用、参数校验、权限审批、trace 和 history 记录。

v3 里还加了几个面试很好讲的 tradeoff：

- `context: inline`：在当前会话里继续跑，能继承上下文，但执行结果会进入当前历史。
- `context: fork`：开隔离子 session 跑 skill，主 session 不被污染，但上下文继承少。
- `allowed-tools`：可以把某个 skill 临时限制成只读工具集。注意它不是给 skill 新增权限，而是在已有工具里做收窄。

所以 v3 的准确表述是：Pico 当前已有 prompt-driven skill loader、catalog 注入、slash 调用、inline/fork 基础链路、allowed-tools 工具面收窄和 skill 事件记录；但它还不是完整动态 skill/plugin 生态。

对应代码：

- `pico/features/skills.py`：skill frontmatter、加载顺序、prompt catalog
- `pico/features/skills_runtime.py`：`invoke_skill()`、inline/fork、`allowed_tools`
- `docs/skills.md`：用户可见的 skill 使用说明
- `tests/test_skills_acceptance.py`：内置 skill、项目 skill、fork、allowed_tools、prompt_only 测试

# 7. 函数调用是什么

函数调用本质上是把模型的自然语言输出变成结构化动作。模型不直接操作系统，而是输出工具名和参数；runtime 解析以后，再决定是否合法、能不能执行。

在 Pico 里，模型输出分两类：

- 工具调用：继续推进任务
- 最终答案：结束本次运行

工具调用有 JSON 和 XML 两种形式。JSON 适合简单参数，XML 适合多行写入或 patch。

例子：

```text
<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>
```

多行 patch：

```text
<tool name="patch_file" path="README.md">
<old_text>old content</old_text>
<new_text>new content</new_text>
</tool>
```

但真正关键的是后面的校验链：

- 工具名是否存在
- 参数是否符合 schema
- path 是否在 workspace root 内
- risky 工具是否需要审批
- 是否是连续重复调用
- 结果是否需要写入 history、memory、trace、report

tradeoff 在这里：

如果让模型直接输出 shell 或代码，灵活性很高，但不可控。  
如果所有动作都走函数调用，模型自由度下降，但系统可校验、可审计、可恢复。  
Pico 选择后者，因为 code agent 的动作有副作用，必须让模型"提出意图"，而不是直接"拥有执行权"。

对应代码：

- `pico/runtime.py`：`parse()`、`parse_xml_tool()`、`run_tool()`
- `pico/tools.py`：工具定义、示例、参数校验

> 💡 **v3 版本可以补充：**如果面试官把 skill 和 function calling 混着问，可以这样区分：skill 决定"怎么做"，tool/function call 决定"做什么动作"。

skill 本身不会绕过 Pico 的 tool call 协议。比如 `/review` 只是把"请审查 diff、找风险、不要改文件"这类 workflow 注入模型；模型后续如果要读文件，仍然要输出 `read_file` 工具调用；如果要跑命令，仍然要输出 `run_shell`。如果 skill 的 `allowed-tools` 只允许 `read_file`，模型即使请求 `run_shell`，runtime 也会按 `tool_not_allowed` 拒绝。

这个边界很关键：

- skill 不是 MCP，不负责提供新工具。
- skill 不是脚本执行器，不直接改文件。
- skill 是 prompt workflow，最终副作用仍由 tool executor 统一治理。

这就是为什么 Pico 把 skill 放在 prompt/workflow 层，而不是把它做成"直接执行任意代码"的插件层：扩展点仍然回到统一工具协议、权限判断、trace 和 history，而不是另开一条副作用通道。

对应代码：

- `pico/features/skills_runtime.py`：skill 调用后仍走 `agent.ask(prompt)`
- `pico/core/permissions.py`：active tool profile 的权限判断
- `pico/core/tool_executor.py`：权限事件和工具执行结果记录

# 8. 工具如果是合理的重复调用，系统怎么处理

先给面试官分层：

重复调用有两种。

第一种是执行层重复：同一个工具、同样参数、连续发生，而且没有任何新信息增量。这种通常是坏循环。

第二种是语义层重复：不是完全同参，但反复读同一路径、同一主题，导致上下文里堆了很多相似信息。这种不一定要在执行时硬拦，更适合在上下文压缩阶段折叠。

Pico 当前实现的是最保守的一层：只拦最简单的执行层坏循环。

具体来说，如果最近两次工具历史已经是同名同参，再发起第三次同名同参调用，就返回：

```text
repeated identical tool call
```

让模型换工具或给最终答案。

这意味着：

- 第一次读 1-80 行，第二次读 81-160 行，允许
- patch 失败后重新 read_file，允许
- 文件被修改后重新读确认结果，允许
- 最近两次已经完全同参，第三次还照抄，拒绝

这里的 tradeoff 是误杀率和治理力度。

如果第二次同参就拦，系统更激进，但可能误杀"模型确认信息"的合理行为。  
如果完全不拦，模型可能陷入无效循环。  
Pico 当前选择第三次才拦，是一个轻量防护：先不影响正常探索，但挡住明显卡住的循环。

不过我也会承认：这只是执行层治理。更完整的重复读取治理，还应该在上下文层统计同路径读取次数、摘要复用次数、折叠次数，并把这些写进 benchmark 指标里。

对应代码：

- `pico/runtime.py`：`repeated_tool_call()`
- `benchmarks/coding_tasks.json`：`repeated_read_recovery`

# 9. 模型输出不稳定怎么处理

Pico 里至少处理三类不稳定。

第一类是格式不稳定。模型可能没有返回合法 `<tool>` 或 `<final>`，或者工具 JSON 解析失败。Pico 会返回 runtime notice，让模型重新用合法格式输出。

第二类是工具失败。比如路径不存在、参数缺失、patch 文本不唯一、shell 非 0 退出。Pico 不会直接崩，而是把错误结果写回 history，让模型下一轮基于错误继续修。

第三类是循环失控。如果模型一直不给 final answer，或者一直输出坏格式，AgentLoop 有 `max_steps` 和 `max_attempts`，到边界后用明确 stop reason 停下来。

这里的设计取舍是：

- 只要失败就终止：安全，但恢复能力差
- 无限 retry：看起来智能，但容易卡死和烧成本
- 有限 retry + 错误回灌：让模型有机会修正，同时给系统硬边界

Pico 选择第三种。

> 💡 这题可以强调：工具失败本身不是任务失败。工具失败是下一轮模型的观察结果。只有正常 final answer、步数上限、重试上限这类明确终态，才会结束一次运行。

对应代码：

- `pico/runtime.py`：`parse()`、`retry_notice()`
- `pico/agent_loop.py`：`max_steps`、`max_attempts`
- `pico/task_state.py`：`step_limit_reached`、`retry_limit_reached`

# 10. 模型参数给错怎么处理

这题其实是在问工具层是不是 fail-closed。

Pico 的工具不是模型说什么就执行。每个工具都有 schema 和校验逻辑：

- `read_file`：path 必须是文件，行号区间合法
- `search`：pattern 不能为空
- `run_shell`：command 不能为空，timeout 必须在 1 到 120
- `write_file`：不能写到目录
- `patch_file`：old_text 必须非空，并且只能命中一次
- 所有 path 都必须在 workspace root 内，防止 `../` 和 symlink 逃逸

如果参数错了，`validate_tool()` 会抛错，`run_tool()` 会把错误整理成文本返回给模型。如果工具有示例调用，还会附 example，让模型下一轮能修。

一个典型例子是 `patch_file`：

如果模型只给 `old_text`，没给 `new_text`，拒绝。  
如果 `old_text` 在文件里出现两次，拒绝。  
如果路径逃出 workspace，拒绝。

这里的 tradeoff 是：

如果工具层帮模型猜，比如 old_text 出现两次就替换第一处，短期更容易"跑完"；但副作用很危险，因为 agent 可能改错地方。  
Pico 选择 fail-closed：只要无法确定，就拒绝，让模型重新读取上下文或给更精确的 patch。

> ✅ 面试里可以说：工具层宁可拒绝不确定修改，也不要替模型猜。因为代码修改不是聊天回答，错一次会污染工作区。

# 11. Freshness 校验是什么意思

Pico 会在 memory 里保存文件摘要。比如 agent 读过一个文件以后，系统可能会保存一个短摘要，下一轮不用把完整文件内容再塞进 prompt。但代码仓库是活的，文件可能已经被用户、agent 或其他进程修改。如果继续信旧摘要，就会把系统带偏。

所以 Pico 在保存文件摘要时，会记录文件当时的 sha256。恢复任务或继续使用旧摘要时，重新计算当前文件 sha256。如果不一致，就说明旧摘要 stale。

这时系统不会继续无条件信旧摘要，而是：

- 标记 stale path
- resume 状态变成 `partial-stale`
- 创建 checkpoint
- 提醒模型重新读取关键文件

为什么不用 mtime？

mtime 只能说明文件可能变过，hash 更直接表示内容是否一致。对 code agent 来说，我们真正关心的是内容事实，而不是文件时间。

这里的 tradeoff 是成本和正确性：

- 每次都重新读文件，最安全，但上下文和工具成本高
- 永远相信摘要，成本低，但风险最大
- 摘要 + freshness，是折中：没变就复用，变了就失效

Pico 选择第三种。

> 💡 这题可以强调一句：在 code agent 里，没记住最多多读一次；记住了旧事实还继续推理，才是更危险的。

对应代码：

- `pico/memory.py`：文件摘要和 freshness
- `pico/checkpoint.py`：`evaluate_resume_state()`
- `benchmarks/coding_tasks.json`：`freshness_reanchor_resume`
#（注：内容由AI生成）
