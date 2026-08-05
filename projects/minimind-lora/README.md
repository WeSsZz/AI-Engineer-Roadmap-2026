# MiniMind & LoRA Lab

## 目标

通过小型语言模型理解训练与推理链路，并完成一次可复现的领域 SFT/LoRA 实验。本项目重在理解和实验，不追求训练大参数模型。

## 学习检查点

- [ ] 能画出 Decoder-only Transformer 的完整数据流
- [ ] 能解释 Causal Mask、RoPE、RMSNorm、SwiGLU 与 KV Cache
- [ ] 能说明 Pretrain、SFT、LoRA/QLoRA 的目标和差异
- [ ] 能定位训练 loss 异常、过拟合和生成退化
- [ ] 能解释 LoRA rank、alpha、target modules 的影响

## 最小实验

1. 跑通 MiniMind 推理。
2. 用小数据跑通训练或 SFT，验证 loss 与样本生成。
3. 构造并抽检领域指令数据。
4. 完成 LoRA 微调。
5. 对比 Base 与 Fine-tuned 模型，并分析失败案例。

实验记录见 [`experiments.md`](experiments.md)。
