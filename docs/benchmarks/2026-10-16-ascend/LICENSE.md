# ASCEND 数据来源与许可

本目录的 JSON 中包含 ASCEND 人工转写及其评测衍生记录。这些数据文件按 **Creative Commons Attribution-ShareAlike 4.0 International（CC-BY-SA-4.0）** 提供，不属于仓库根目录代码的 MIT 许可。许可全文：[CC-BY-SA-4.0](https://creativecommons.org/licenses/by-sa/4.0/legalcode)。本目录的 `prepare.py` 是 VoxaStage 编写的数据准备脚本，使用仓库 MIT 许可。

原始作品：**ASCEND: A Spontaneous Chinese-English Dataset for Code-switching in Multi-turn Conversation**，Holy Lovenia、Samuel Cahyawijaya、Genta Indra Winata、Peng Xu、Yan Xu、Zihan Liu、Rita Frieske、Tiezheng Yu、Wenliang Dai、Elham J. Barezi、Qifeng Chen、Xiaojuan Ma、Bertram E. Shi、Pascale Fung，LREC 2022。

- [论文与完整作者信息](https://aclanthology.org/2022.lrec-1.788/)
- [官方数据集与许可声明](https://huggingface.co/datasets/CAiRE/ASCEND/blob/737e9800ae31be9932ba8464c80366559bd28424/README.md)
- [官方项目](https://github.com/HLTCHKUST/ASCEND)
- 使用版本：`737e9800ae31be9932ba8464c80366559bd28424`，仅 test split。

变换说明：从固定测试集按预先声明规则抽取 60 条，保持原始参考转写；本地将音频解码并写为 PCM16 WAV，不重采样、不裁剪、不调增益。新增模型识别结果、规范化字符编辑距离、请求计时、哈希和抽样元数据。公开文件不含音频；转写来自原作者人工标注，VoxaStage 未独立逐条复听或修订。

使用、再分发或改编这些数据时，请保留原作署名、来源与许可链接，并说明自己的修改；改编内容需遵循上述相同许可。VoxaStage 不声称构建了 ASCEND 数据集或获得原作者背书。
