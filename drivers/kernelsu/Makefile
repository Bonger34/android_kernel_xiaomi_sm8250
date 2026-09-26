# 冲突模拟夹具（issue #8 的验收用）—— **故意与我们的 drivers/kernelsu/Makefile 冲突**

这不是任何真实的 KernelSU 构建规则。sync-conflict-sim 分支 = 上游 lineage-23.2
的 HEAD（71b13e62）+ **一个提交：在这个路径上放一份内容不同的 Makefile**。

为什么这一定会冲突：本线在分叉之后**新增了** drivers/kernelsu/ 整个目录
（KSU 集成），而这个夹具在**同一个路径**上也新增一份内容不同的文件 ⇒ **add/add 冲突**。

⚠️ 上一版夹具选的是 .github/workflows/build.yml，那是个**坏选择**：那个路径被 GitHub
当成 workflow 解析，一次 push 就触发了一个**跑不起来**的 build run（无 job，状态 failure）。
夹具该选一个**没有副作用**的路径 —— 这一版已经换成 drivers/kernelsu/Makefile。

用法：gh workflow run detect.yml -f upstream_repo=Bonger34/android_kernel_xiaomi_sm8250 -f upstream_ref=sync-conflict-sim
预期：检测器那次 run **红在「试合并」那一步**（退出码 4），且**没有** artifact、
**没有**派发构建、工作分支一个字节没动。