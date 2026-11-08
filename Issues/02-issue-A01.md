## 负责人

@<A账号>

## 目标

两件版型相近的普通短袖，完成正背照 + 三处尺寸，供 Q01 验收，后续交 B 对接。

## 交付物

- [ ] `local-data/garments/top-001/front.jpg`、`back.jpg`
- [ ] `local-data/garments/top-002/front.jpg`、`back.jpg`
- [ ] `local-data/garments/measurements.csv`，两行，表头逐字一致：

```csv
garment_id,length_cm,chest_width_cm,sleeve_length_cm,front_file,back_file
```

## 采集要求

- 平铺拍完整正面/背面，光线均匀，不裁边。
- 尺寸用厘米数值：衣长、腋下平铺胸宽、袖长。
- 编号固定 `top-001`、`top-002`，后续三维资产、推荐结果都用同一编号。

## 交接方式

在本 Issue 下留评论，五段式：

```text
任务编号和负责人
当前状态和对应版本
可检查的结果或报错
运行或复现步骤
下一步、接收人和检查时间
```

清单与照片走内部渠道；**不要**贴照片、不要提交进仓库。

## 验收

Q01：2 个编号 × 5 格 = 10 格（正、背、衣长、胸宽、袖长）全齐为通过。
公开结论只写「A01 已核对 2 件」。

## 检查时间

2026-11-09 18:00
