# redis_offline_migration
从阿里云一键迁移Redis到AWS. 并行多进程处理. 

## 功能
1. 支持跨版本迁移, 已验证从阿里☁️ 2.8/4.0 版本可以成功迁移到AWS 4.0.10 版本(5.0 往 4.0 迁移不被支持);
2. 阿里☁️ AccessKey 安全保存在 AWS Systems Manager Parameter Store;
3. 从阿里云自动创建 Redis 备份, 下载RDB备份文件, 上传到S3, 然后基于该文件创建 AWS Redis 实例;
4. 目前只支持主从，集群版本未支持;
5. 附带了阿里云实例创建、网络配置方法, 使用 AWS 公共数据集进行填充

## 性能
1. 迁移单个 3.5GB 内存数据（压缩后的 RDB 文件 680MB）的阿里云实例到 AWS 耗时 10 分 20 秒;
2. 迁移 10 个同规格的实例耗时 28分12秒

## 注意
1. AWS 2.8 版本不能开启 TLS/AES, 而 AUTHTOKEN 依赖于 TLS;
2. 建议运行在 EC2 上, 予绑定的 Role创建 Redis 实例和上传 S3 文件以及读 SSM Parameter Store 的权限;
3. Psync 阿里要求 4.0 以上版本, AWS 要求 ElastiCache 5.0.5 以上版本;
4. 阿里云客户端有时报 SignatureNonceUsed 错误, 文档称是随机数重复