# 故障复现工具

这两个脚本用来在**真机之外**先单独把「延迟旧响应、哈希错误、坏文件」这三类问题跑通，
不用连真实后端、不用真实照片，也不用装任何第三方库，Python 自带的功能就够了。

## 有什么用

| 脚本 | 干什么 |
| --- | --- |
| `make_fault_package.py` | 生成三份「候选包」，默认直接拿仓库里的真实两衣包当素材 |
| `fault_server.py` | 把候选包当成一个假后端跑起来，接口和真后端长得一样 |

三份候选包对应验收里的三个场景：

| 目录 | catalog_version | 场景 | 客户端该有的表现 |
| --- | --- | --- | --- |
| `good/` | `fixture-face-03` | 一切正常 | 正常下载、解出上屏 |
| `sha-mismatch/` | `fixture-face-03-sha-bad` | 哈希对不上（E03） | 下载完发现哈希不对，拒绝、保留旧衣服 |
| `corrupt-glb/` | `fixture-face-03-corrupt` | 哈希对得上但文件是坏的（E04） | 解码失败，不上屏、保留旧衣服 |

三个目录故意用不同的 `catalog_version`，这样客户端会把它们当成一次新的发布，
走完整的下载/校验/解码流程，而不是命中本地旧缓存。

## 素材从哪来

`make_fault_package.py` 默认自动找仓库里的真实候选包：

```
local-data/handoff/T02-candidate-20261113/
```

找到就复制里面的 `person-001.glb`、`top-001_v2.glb`、`top-002_v1.glb` 当素材，
所以 `good/` 是**真正能解码、能上屏**的 GLB；找不到才退回占位字节（只能验证哈希和解码失败两条路径）。

要换素材就加 `--source 某个候选包目录`。比如用 fixture-face-01 那套：

```bash
python server/tools/make_fault_package.py --source outputs/server-assets/person-001/fixture-face-01
```

## 怎么用

在仓库根目录下跑（`python` 换成你自己的 Python）：

```bash
# 生成三份候选包，落在 outputs/fault-fixtures/{good,sha-mismatch,corrupt-glb}/
python server/tools/make_fault_package.py
```

再起假后端。三种情况各起一个，端口错开：

```bash
# 1. 正常（--fixtures 不写时默认就是 good）
python server/tools/fault_server.py --port 8001

# 2. 哈希错
python server/tools/fault_server.py --fixtures outputs/fault-fixtures/sha-mismatch --port 8002

# 3. 坏文件（哈希能过、解码会挂）
python server/tools/fault_server.py --fixtures outputs/fault-fixtures/corrupt-glb --port 8003
```

让 App 把「服务器地址」指到 `http://127.0.0.1:8001`（或 8002、8003）就能测。
注意手机连电脑要用局域网 IP，`127.0.0.1` 只在本机模拟器里有意义。

## 复现「延迟旧响应被丢弃」（E02）

用 `--delay` 让推荐接口先睡几秒再回，模拟网络慢：

```bash
python server/tools/fault_server.py --fixtures outputs/fault-fixtures/good --port 8001 --delay 5
```

用户点推荐后，趁这 5 秒赶紧换衣服；迟到的旧推荐回来后，App 应该把它丢掉、不再替换当前衣服。

## 其它参数

| 参数 | 说明 |
| --- | --- |
| `--token abc` | 要求客户端带 `Authorization: Bearer abc`，不带就 401 |
| `--recommend top-002` | 推荐接口固定返回 top-002（默认 top-001） |
| `--recommend no_match` | 推荐接口固定返回「无匹配」 |
| `--port 8001` | 监听端口（默认 8001） |
| `--delay 5` | 推荐接口延迟几秒再回（复现 E02） |
| `--fixtures DIR` | 候选包目录（默认 `outputs/fault-fixtures/good`） |

## 说明

- `good/` 现在默认是真实 GLB，能真正走完「下载 → 校验 → 解码 → 上屏」；只有找不到真实候选包时才用占位字节。
- `sha-mismatch/` 的文件是真的，只是 `manifest.json` 里 top-001 的 `sha256` 被写成了 64 个 0。
- `corrupt-glb/` 里 top-001 的字节是坏数据，但 `manifest.json` 的 `sha256` 是按坏字节算的——所以哈希能过、解码必挂，正好对应 E04。

## 校验器信谁：文件、manifest、validation-report 三方对齐

`import_candidate.py` 校验时按这个顺序对：

1. **文件实际算出来的 sha256 / 体积** 是基准；
2. 跟 **`manifest.json` 里声明的** 比，不一致就报「和 manifest 声明不一致」（E03 走这条）；
3. 如果包里还带了 **`validation-report.json`**，再跟它比一次，不一致报「和 validation-report 声明不一致」。

注意一个坑：A 交过来的真实候选包里，`manifest.json` **本来就不写 sha256**（哈希只写在 `validation-report.json` 里）。所以校验器在 manifest 没写哈希时，会退回用 validation-report 的声明值来核对。两边都没有声明时，就只能做到「文件存在 + GLB 能解码」这一层——这种情况会照常放行，但强度有限，正式发布应当走带哈希的完整校验。

自检命令（都带 `--dry-run`，不写库不拷文件）：

```bash
python server/tools/import_candidate.py outputs/fault-fixtures/good        --dry-run   # exit 0
python server/tools/import_candidate.py outputs/fault-fixtures/sha-mismatch --dry-run  # exit 1，报 sha256 与 manifest 声明不一致
python server/tools/import_candidate.py outputs/fault-fixtures/corrupt-glb  --dry-run  # exit 1，报解不了
```
