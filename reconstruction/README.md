# 人物工具与本地预览

本目录包含项目编写的模型调用、网格导出、外观投影及联调工具。运行模型需要本地已配置的 PIXIE 源码、权重和 SMPL-X 数据。Android 队员可直接使用队长交付的内部 GLB 包。

## 工具对应关系

| 文件 | 输入 | 产物 |
| --- | --- | --- |
| run_pixie_mesh.py | 单张全身照片 | 原姿势和 T 姿势 GLB/OBJ、运行记录 |
| tune_shape_pixie.py | 全身照片、身高、扫描参数 | 同尺度截面测量及体型候选 |
| export_person_asset.py | 人物 GLB、可选身高 | 脚底归零的 GLB 和变换记录 |
| project_face_color.py | 固定人物、正面近照 | 顶点着色 GLB、近景对照；顶点与三角面保持一致 |
| build_tryon_fixture.py | 固定 T 姿势人物 | 两件合成测试短袖及 manifest |
| preview_person_asset.py | 一或两件人物资产 | 同尺度正侧背面图片 |
| preview_tryon.py | 完整资产包 | 可旋转和换衣的 Tk 窗口，或三视图图片 |

## 已有模型环境

在仓库根目录打开 PowerShell，将下面两个路径替换为自己已有的模型环境与授权依赖目录。

```powershell
$modelPython = 'C:\your-model-env\Scripts\python.exe'
$env:OOTD_PIXIE_ROOT = 'C:\your-licensed-model-directory'
```

模型目录应含 `pixie-5c8879b/pixielib/`、`pixie_model.tar`、`SMPLX_NEUTRAL_2020.npz` 和 `utilities.zip`。队长当前源码基于 PIXIE `5c8879b`，另有本地依赖兼容修改。新机器需先核对官方许可及安装说明，完成模型环境验证后再执行推理。

已验证模型环境使用 Python 3.12、PyTorch 2.8.0+cu129、torchvision 0.23.0、NumPy 2.2.6、SciPy 1.15.3、trimesh 4.7.4、OpenCV headless 4.11.0.86、Pillow、kornia、yacs。Tk 窗口还需要本机 Tcl/Tk 运行库。模型环境与后端环境独立。

### 生成与校准

```powershell
& $modelPython .\reconstruction\run_pixie_mesh.py --image .\local-data\people\person-001\front.jpg --output .\outputs\person-trial
# 将 height-m 替换为本人提供的米制身高
& $modelPython .\reconstruction\export_person_asset.py --input .\outputs\person-trial\person-tpose.glb --output-dir .\outputs\person-aligned --height-m 1.70
```

有裁剪需求时，`--bbox L T R B` 使用 EXIF 转正后的照片坐标。记录实际输入、参数和结果，输出目录每次采用新名称。

### 体型与外观试验

```powershell
& $modelPython .\reconstruction\tune_shape_pixie.py --pixie-dir $env:OOTD_PIXIE_ROOT --image .\local-data\people\person-001\front.jpg --height-m 1.70 --output-dir .\outputs\shape-trial
& $modelPython .\reconstruction\project_face_color.py --pixie-dir $env:OOTD_PIXIE_ROOT --person .\outputs\person-aligned\person-001.glb --image .\local-data\people\person-001\face.jpg --output-dir .\outputs\face-trial
```

体型脚本默认以基线腰部截面缩窄 8% 作为试验目标，使用同一身高比较候选。该数值用于试验筛选，本人确认后才作为衣物适配基准。面部脚本将近照颜色投影到网格，侧后部使用近似肤色，头发几何需继续制作。

### 包和预览

取得内部包后可直接预览。使用 Tk 正常的本机 Python，并安装 numpy、trimesh、Pillow、opencv-python-headless；无需加载模型权重。

```powershell
$package = '.\outputs\handoff\person-001\fixture-face-01'
& $modelPython .\reconstruction\preview_tryon.py $package
& $modelPython .\reconstruction\preview_tryon.py $package --output .\outputs\tryon-preview.png
```

如需从已对齐人物生成合成测试衣物：

```powershell
& $modelPython .\reconstruction\build_tryon_fixture.py --person .\outputs\person-aligned\person-001.glb --output-dir .\outputs\test-package --catalog-version fixture-new
```

当前包的三件资产共同采用米制、Y 向上、正面 +Z、脚底中心原点和固定 T 姿势。人物体型或衣物改变后生成新的目录版本。测试短袖由人体外扩裁切得到，真实衣物需另按照片和尺寸制作。

## 记录与来源

源码可公开，个人照片、模型结果、测量参数与调试叠图存于已忽略的目录。第三方依赖由使用者按官方条件获取。

- [PIXIE 源码与许可](https://github.com/yfeng95/PIXIE)
- [SMPL-X 官方获取入口](https://smpl-x.is.tue.mpg.de/)
- [后端资产校验及导入](../server/README.md)
