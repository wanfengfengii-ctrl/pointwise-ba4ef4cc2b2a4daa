# 海栈站原票交班

晚班从白班交出的原票继续确认。班长按实际确认归属签出一份全票核账。原票的原件名单、分页取件和出处验收见 README.md。原位从零起算，区间右端不含在内。

## Python 入口

| 入口 | 输入与返回 |
| --- | --- |
| `delivery.ticket.enable_handoff(root, ticket_number, day_shift, evening_shift)` | 为已开的票固定两个不同的班次标识，白班从原位 0 开始。返回 `None`。同票同班次重投保留已有进度，异参拒绝。 |
| `delivery.ticket.confirm(root, ticket_number, shift, request_id, position, lines, *, before_commit=None)` | 提交从 `position` 起的非空有序 `DeliveryLine` 序列，例如原 `page` 的 `.lines`。返回确认回执，字段为 `ticket_number`、`shift`、`request_id`、`start` 和 `stop`。 |
| `delivery.ticket.take_over(root, ticket_number, request_id, *, before_commit=None)` | 完成一次白班到晚班的接手。返回接手回执，字段为 `ticket_number`、`request_id`、`next_position`。`next_position` 是接手生效时的首个未确认原位，满票接手时取名单长度。 |
| `audit.close.close_joint(root, ticket_number, *, before_commit=None)` | 按票签发联合账，返回 `record` 和 `originals`。`record` 沿用核账号、票号、卷号、明细和整数总量字段。每条 `record.lines` 保留 V1 的 `original_id`、`origin`、`reading`、`digest`，另以 `confirmation` 提供该原位所据的完整确认回执，班次可从 `confirmation.shift` 查取。`originals` 为同序原件行。 |
| `audit.history.readback(root, audit_number)` | 使用原入口按同一核账号空间取回普通账或联合账。联合账读回保持上述字段与确认归属。 |

## 确认与接手

班次和请求号使用 V1 的非空白 UTF-8 文本规则。`position` 是非负整数。首次确认须在票上启用双班后，由当前有资格的班次连续提交，起点等于首个未确认原位，终点至多为名单长度。整段逐位按该票名单和接纳清单核对身份、出处、整数读数及原字节摘要，整段一起确认。确认归属以受理时的票号为准。原分页的 `DeliveryLine` 是原件值，两个票上的等值原件行可以分别核验。每份确认只覆盖回执所列票号的原位。

接手提交后由晚班确认余下部分，确认和接手以目录提交先后裁决。接手成功回执中的原位保持为实际交班位置，晚班继续确认后仍按原值返回。确认与接手在同票共用请求号。同票同号的已成功操作，类型及全部业务参数匹配时返回原回执，包括交班后白班确认的重投。对确认，业务参数含班次、起点及有序交付行，行相等按 V1 验收字段判断。同票同号异参、第二个不同请求号的接手、越位、缺口、原件不符及无资格首次确认均抛 `ValueError`。未知票沿用 `UnknownTicket`。

## 联合签发

签发条件是已经接手且原票每个位置恰有一份有效确认。只能采用同票的已提交确认。未启用、未接手或覆盖不齐时抛 `ValueError`。已接手的空票可签零账。签发按原票顺序固定每行确认及原件依据，逐件用精确整数求和。同票再次调用 `close_joint` 返回同一核账号和明细。联合账读回沿原介质取件语义获取原字节，按封存记录提供整数总量与逐位确认回执。普通账继续按原接口签发及读回。已有库可沿 README.md 重复初始化后启用本功能。

确认、接手与签发的首次成功在各自提交时生效。提交失败保持本次操作之前的位置、资格和已签成果。可选 `before_commit(db)` 沿用接纳和改序的故障注入约定，在首次写入结果后、提交前接收该次目录连接。测试可传 `tests.fixtures.deny_commit` 拒绝 SQLite 提交。失去成功回包的场景可在操作提交后丢弃其返回，随后用原业务参数重投。SQLite 或注入异常可沿原类型传播。

## 海栈样例与运行

`handoff_cases.py` 复用 `samples/haizhan.json`。白班 `day` 先以 `day-A` 确认 A，B 与晚班 `evening` 的接手发生竞争。可在交班期间把盘面改排成 B、C、A，继续用原票按 A、B、C 核账。

| 目录提交顺序 | 接手位置 | 原序 A、B、C 的确认请求 | 总量 |
| --- | --- | --- | --- |
| 白班 `day-B` 先成功，返回丢失 | 2 | `day-A`, `day-B`, `evening-C` | 31 |
| `handoff` 先成功，白班首次 `day-B` 被拒 | 1 | `day-A`, `evening-BC`, `evening-BC` | 31 |

第一案交班后重投 `day-B`，仍得到原票上 `[1,2)` 的白班回执。第二案由晚班一次确认 `[1,3)`，B、C 引用同一回执。两案在齐备之前均拒绝联合签发。闭合后重新连接，用核账号读取已签明细和总量。可用独立调用执行两种确定的提交顺序，也可在先行操作的 `before_commit` 暂停时发起另一请求。

基线运行清单 `.pointwise-runtime.json` 使用版本 1 和 `cli` 入口类型。`setup` 是准备命令的参数数组列表，`verify` 与 `demo` 各是一个命令参数数组。沿用该清单交付完整原回归和新增业务核验，失败以非零退出。演示实际执行上述两种交班次序、回执处理及重开领取。附简短说明，写清联合签发如何认定原位、有效确认和交班边界。`python3 -B handoff_cases.py` 可独立执行原 V1 对照。
