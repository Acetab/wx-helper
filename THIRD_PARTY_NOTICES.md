# 第三方组件

原生桥接使用 MinHook 1.3.4，并保留构建所需的源码、头文件、作者信息及原始许可证：

- `native/vendor/minhook-1.3.4/LICENSE.txt`
- `native/vendor/minhook-1.3.4/AUTHORS.txt`
- `native/vendor/minhook-1.3.4/src/hde/` 下的原始版权声明

保留的 DLL 由本项目桥接源码和上述组件构建；不包含微信客户端程序或微信核心 DLL。

成语接龙的扩展词表来自 [pwxcoo/chinese-xinhua](https://github.com/pwxcoo/chinese-xinhua)，Copyright (c) 2018 PWXCOO，MIT License。仅提取四字词条；固定版本、筛选规则与校验值见 `plugins/idiom_chain/data/README.md`，完整许可证见 `plugins/idiom_chain/data/LICENSE.chinese-xinhua.txt`。
