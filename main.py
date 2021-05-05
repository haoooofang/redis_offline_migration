"""
阿里☁️ Redis 往 AWS 的离线迁移脚本
通过 RDB 备份文件进行迁移。
先在阿里☁️形成手工备份，下载该RDB格式备份文件并上传到S3，基于这个备份文件生成新的 Redis 实例。
目前仅支持主从配置。
"""

import json
from datetime import datetime, timedelta
from time import sleep
import multiprocessing
from requests import get
import boto3
import certifi
import redis
import logging
from typing import List, Tuple

from alibabacloud_r_kvstore20150101.client import Client as R_kvstore20150101Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_r_kvstore20150101 import models as r_kvstore_20150101_models

REGION = 'ap-northeast-1'
PASSWORD = 'AliyunMigration#passw0rd'
BUCKET_NAME = 'hao-nrt-workshop'

logging.basicConfig(filename='redis_migration.log', level=logging.INFO)


class RedisMigration:
    def __init__(self):
        pass

    @staticmethod
    def get_ext_ip() -> str:
        return get('https://api.ipify.org').text

    @staticmethod
    def get_ali_ak_pair() -> Tuple[str, str]:
        """
        获取阿里云 AK/SK
        阿里云 Accesskey_Id 和 Accesskey_secret 请先保存在 AWS Systems Manager 的 Parameter Store 中
        名称为:
            ali_ak_pair
        值格式为:
            {"accesskey_id":"", "accesskey_secret":""}
        权限：
            确认拥有该Key的用户具有上面 import 里列出的 Redis 权限，去掉 Request 尾缀就是操作名称
        """
        sess = boto3.Session(region_name=REGION)
        ssm = sess.client('ssm')
        response = ssm.get_parameter(
            Name='ali_ak_pair',
            WithDecryption=True
        )
        ali_ak_pair = json.loads(response.get('Parameter').get('Value'))
        ak = ali_ak_pair.get('accesskey_id')
        sk = ali_ak_pair.get('accesskey_secret')
        return ak, sk

    @staticmethod
    def create_ali_redis_client(
            access_key_id: str,
            access_key_secret: str,
    ) -> R_kvstore20150101Client:
        """
        使用AK&SK初始化账号Client
        :param access_key_id:
        :param access_key_secret:
        :return: Client
        """
        config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret
        )
        config.endpoint = 'r-kvstore.ap-northeast-1.aliyuncs.com'
        return R_kvstore20150101Client(config)

    @staticmethod
    def create_ali_redis_instances(num=1) -> List[str]:
        """
        在阿里云创建源库
        :param num: 数量
        :return: ali_instance_ids
        """
        ak_, sk_ = RedisMigration.get_ali_ak_pair()
        ali_redis_client = RedisMigration.create_ali_redis_client(ak_, sk_)
        instance_ids = []

        for i in range(num):
            create_instance_request = r_kvstore_20150101_models.CreateInstanceRequest(
                region_id=REGION,
                zone_id='ap-northeast-1a',
                instance_class='redis.master.stand.default',
                password=PASSWORD,
                engine_version='4.0'
            )
            res = ali_redis_client.create_instance(create_instance_request)
            instance_id = res.body.instance_id
            instance_ids.append(instance_id)
            logging.info(instance_id)
        return instance_ids

    @staticmethod
    def get_ali_region() -> None:
        ak_, sk_ = RedisMigration.get_ali_ak_pair()
        ali_redis_client = RedisMigration.create_ali_redis_client(ak_, sk_)

        describe_regions_request = r_kvstore_20150101_models.DescribeRegionsRequest()
        res = ali_redis_client.describe_regions(describe_regions_request)
        print(res.body.region_ids.kvstore_region)

    @staticmethod
    def get_ali_redis_instances() -> List[str]:
        """
        取得阿里云当前区域所有 Redis 实例 ID
        :return: ali_instance_ids
        """
        ak_, sk_ = RedisMigration.get_ali_ak_pair()
        ali_redis_client = RedisMigration.create_ali_redis_client(ak_, sk_)

        describe_instances_request = r_kvstore_20150101_models.DescribeInstancesRequest()
        res = ali_redis_client.describe_instances(describe_instances_request)
        ali_instance_ids = [i.instance_id for i in res.body.instances.kvstore_instance]
        logging.info(ali_instance_ids)
        return ali_instance_ids

    @staticmethod
    def wait_ali_instance_state(ali_instance_id) -> None:
        """
        等待实例状态正常
        :param ali_instance_id:
        :return: None
        """
        ak_, sk_ = RedisMigration.get_ali_ak_pair()
        ali_redis_client = RedisMigration.create_ali_redis_client(ak_, sk_)

        status = 'Unknown'
        while status != 'Normal':
            try:
                describe_instance_attribute_request = r_kvstore_20150101_models.DescribeInstanceAttributeRequest(
                    instance_id=ali_instance_id
                )
                res = ali_redis_client.describe_instance_attribute(describe_instance_attribute_request)
                status = res.body.instances.dbinstance_attribute[0].instance_status
                logging.info(status)
            except Exception:
                pass
            sleep(20)

    @staticmethod
    def config_ali_instance(ali_instance_id) -> None:
        """
        设置阿里云实例
        :param ali_instance_id:
        :return: None
        """
        ak_, sk_ = RedisMigration.get_ali_ak_pair()
        ali_redis_client = RedisMigration.create_ali_redis_client(ak_, sk_)

        ext_ip = RedisMigration.get_ext_ip()

        RedisMigration.wait_ali_instance_state(ali_instance_id)

        # 等待实例状态正常后修改白名单，添加本地到 aws 白名单组
        modify_security_ips_request = r_kvstore_20150101_models.ModifySecurityIpsRequest(
            instance_id=ali_instance_id,
            security_ips=ext_ip,
            modify_mode='cover',
            security_ip_group_name='aws'
        )
        res = ali_redis_client.modify_security_ips(modify_security_ips_request)
        logging.info(res.body.request_id)

        RedisMigration.wait_ali_instance_state(ali_instance_id)

        # 生成公网地址，域名前缀是 aws-migration- + 实例ID
        # 阿里☁️公网地址会生成额外的 Connection 属性对，通过判断数量来决定是否已有公网地址
        describe_instance_attribute_request = r_kvstore_20150101_models.DescribeInstanceAttributeRequest(
            instance_id=ali_instance_id
        )
        res = ali_redis_client.describe_instance_attribute(describe_instance_attribute_request)
        attributes = res.body.instances.dbinstance_attribute
        if len(attributes) < 2:
            port = attributes[0].port
            allocate_instance_public_connection_request = r_kvstore_20150101_models. \
                AllocateInstancePublicConnectionRequest(
                    instance_id=ali_instance_id,
                    connection_string_prefix='aws-migration-' + ali_instance_id,
                    port=port
                )
            res = ali_redis_client.allocate_instance_public_connection(allocate_instance_public_connection_request)
            logging.info(res.body.request_id)

        RedisMigration.wait_ali_instance_state(ali_instance_id)

        # 查看 Redis 内容
        describe_instance_attribute_request = r_kvstore_20150101_models.DescribeInstanceAttributeRequest(
            instance_id=ali_instance_id
        )
        res = ali_redis_client.describe_instance_attribute(describe_instance_attribute_request)
        port = res.body.instances.dbinstance_attribute[1].port
        url = res.body.instances.dbinstance_attribute[1].connection_domain

        ali_redis = redis.Redis(host=url, port=port, db=0, password=PASSWORD)
        logging.info(ali_redis.scan())

        # 用 S3 中的 CSV 文件填充 Redis，文件小于 500MB，总容量小于 3.5GB
        total_size = 0
        sess = boto3.Session(region_name=REGION)
        s3 = sess.resource('s3')
        bucket = s3.Bucket('nyc-tlc')
        # 纽约出租车公开数据集
        for obj in bucket.objects.all():
            if obj.key.startswith('trip data') \
                    and obj.key.endswith('csv') \
                    and obj.size < 500 * 1024 * 1024 \
                    and obj.size + total_size < 3.5 * 1024 * 1024 * 1024:
                response = obj.get()
                ali_redis.set(obj.key, response.get('Body').read())
                total_size += obj.size
                logging.info(obj.key)
        logging.info(ali_redis.scan())
        logging.info(total_size)
        # r.flushall()

    @staticmethod
    def create_rdb(ali_instance_id) -> Tuple[str, str]:
        """
        备份阿里云实例
        :param ali_instance_id:
        :return: (ali_instance_id, file_name)
        """
        ak_, sk_ = RedisMigration.get_ali_ak_pair()
        ali_redis_client = RedisMigration.create_ali_redis_client(ak_, sk_)

        # 手工进行 RDS 备份
        create_backup_request = r_kvstore_20150101_models.CreateBackupRequest(
            instance_id=ali_instance_id
        )
        res = ali_redis_client.create_backup(create_backup_request)
        backup_job_id = res.body.backup_job_id
        logging.info(backup_job_id)

        # 得到下载地址，排列在前的为最近的备份，假设备份能在 30 分钟内完成，否则调节 startTime
        describe_backup_request = r_kvstore_20150101_models.DescribeBackupsRequest(
            instance_id=ali_instance_id,
            start_time=(datetime.utcnow() - timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%MZ'),
            end_time=(datetime.utcnow() + timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%MZ'),

        )
        res = ali_redis_client.describe_backups(describe_backup_request)
        while len(res.body.backups.backup) == 0:
            sleep(20)
            try:
                res = ali_redis_client.describe_backups(describe_backup_request)
            except Exception:
                pass

        backup_download_url = res.body.backups.backup[0].backup_download_url
        file_name = backup_download_url.split('/')[-1].split('?')[0]
        rdb_file = get(backup_download_url)
        logging.info(backup_download_url)

        sess = boto3.Session(region_name=REGION)
        s3 = sess.resource('s3')
        bucket = s3.Bucket(BUCKET_NAME)

        s3.meta.client.put_object(Bucket=bucket.name, Key=file_name, Body=rdb_file.content)
        logging.info(s3.ObjectSummary(bucket.name, file_name).size)

        # 设置在 S3 中 RDB 文件的 ACL，允许 ElastiCache 服务访问，不适合香港等 2019年3月20日之后上线的区域
        object_acl = s3.ObjectAcl(bucket.name, file_name)
        object_acl.put(
            GrantRead='id=540804c33a284a299d2547575ce1010f2312ef3da9b3a053c8bc45bf233e4353',
            GrantReadACP='id=540804c33a284a299d2547575ce1010f2312ef3da9b3a053c8bc45bf233e4353',
        )
        return ali_instance_id, file_name

    @staticmethod
    def del_ali_instance(ali_instance_id) -> None:
        """
        删除阿里云 Redis 实例
        备份成功上传后，就可以删除阿里☁️ Redis 实例了，即使是手动备份，实例删除后也不存在，这与 AWS 不同
        :param ali_instance_id:
        :return:
        """
        ak_, sk_ = RedisMigration.get_ali_ak_pair()
        ali_redis_client = RedisMigration.create_ali_redis_client(ak_, sk_)

        delete_instance_request = r_kvstore_20150101_models.DeleteInstanceRequest(
            instance_id=ali_instance_id
        )
        res = ali_redis_client.delete_instance(delete_instance_request)
        logging.info(res.body.request_id)

    @staticmethod
    def create_aws_instance(tuple1) -> str:
        """
        创建新的 AWS Redis 实例, 需要已经建立的子网组, 安全组, 参数组
        :param tuple1:
        :return: replica_grp_id
        """
        aws_instance_id, file_name = tuple1
        sec_grp_id = 'sg-0f3bd26533c0af72d'
        para_grp_name = 'default.redis4.0'
        subnet_grp_name = 'sg-pri'
        node_group_id = '0001'

        sess = boto3.Session(region_name=REGION)
        s3 = sess.resource('s3')
        bucket = s3.Bucket(BUCKET_NAME)
        elasticache = sess.client('elasticache')

        res = elasticache.create_replication_group(
            ReplicationGroupId=aws_instance_id,
            ReplicationGroupDescription='Migration from Aliyun',
            NumNodeGroups=1,
            ReplicasPerNodeGroup=1,
            NodeGroupConfiguration=[
                {
                    'NodeGroupId': node_group_id,
                },
            ],
            CacheNodeType='cache.r5.large',
            Engine='redis',
            EngineVersion='4.0.10',
            CacheParameterGroupName=para_grp_name,
            CacheSubnetGroupName=subnet_grp_name,
            SecurityGroupIds=[
                sec_grp_id,
            ],
            SnapshotArns=[
                'arn:aws:s3:::' + bucket.name + '/' + file_name,
            ],
            AuthToken=PASSWORD,
            TransitEncryptionEnabled=True,
            AtRestEncryptionEnabled=True,
        )
        replica_grp_id = res.get('ReplicationGroup').get('ReplicationGroupId')
        logging.info(replica_grp_id)

        # 等待创建完成
        status = 'unknown'
        while status != 'available':
            try:
                res = elasticache.describe_replication_groups(
                    ReplicationGroupId=replica_grp_id,
                )
                status = res.get('ReplicationGroups')[0].get('Status')
                logging.info(status)
            except Exception:
                pass
            sleep(20)

        # 检查实例内容
        res = elasticache.describe_replication_groups(
            ReplicationGroupId=replica_grp_id,
        )
        endpoint = res.get('ReplicationGroups')[0].get('NodeGroups')[0].get('PrimaryEndpoint').get('Address')
        port = res.get('ReplicationGroups')[0].get('NodeGroups')[0].get('PrimaryEndpoint').get('Port')
        aws_redis = redis.Redis(host=endpoint, port=port, db=0, ssl=True, ssl_ca_certs=certifi.where(),
                                password=PASSWORD)
        # aws_redis = redis.Redis(host=endpoint, port=port, db=0)
        logging.info(aws_redis.scan())
        return replica_grp_id

    @staticmethod
    def del_aws_instance(replica_grp_id) -> None:
        """
        删除 AWS Redis 实例
        :param replica_grp_id:
        :return: None
        """
        sess = boto3.Session(region_name=REGION)
        elasticache = sess.client('elasticache')

        res = elasticache.delete_replication_group(
            ReplicationGroupId=replica_grp_id,
        )
        logging.info(res.get('ReplicationGroup'))

    @staticmethod
    def main():
        # 阿里云源环境准备, 创建实例
        num = 10
        ids = RedisMigration.create_ali_redis_instances(num)
        print("Instances {} are created".format(ids))

        # 更改实例网络设置, 填充实例
        process_jobs = []
        for id_ in ids:
            p = multiprocessing.Process(target=RedisMigration.config_ali_instance, args=(id_,))
            process_jobs.append(p)
            p.start()
        for p in process_jobs:
            p.join()
        print("Instances {} are ready for work.".format(ids))

        start = datetime.utcnow()
        # 备份实例
        pool = multiprocessing.Pool(processes=num)
        results = pool.map_async(RedisMigration.create_rdb, ids).get()
        pool.close()
        pool.join()
        for (ali_instance_id_, file_name_) in results:
            print("Instance {} is uploaded RDS file {} to S3.".format(ali_instance_id_, file_name_))

        # 恢复到 AWS 实例
        pool = multiprocessing.Pool(processes=num)
        rg_ids = pool.map_async(RedisMigration.create_aws_instance, results).get()
        pool.close()
        pool.join()
        for r in rg_ids:
            print("Elasticache instance {} is created.".format(r))

        end = datetime.utcnow()
        elapse = end - start
        print("We spent {} for migrating {} instances".format(elapse, num))

        # 删除阿里云实例
        for id_ in ids:
            RedisMigration.del_ali_instance(id_)
            print("AliCloud redis instance {} is deleted".format(id_))

        # 删除 AWS 实例
        for rg_id_ in rg_ids:
            RedisMigration.del_aws_instance(rg_id_)
            print("AWS Elasticache instance {} is deleted".format(rg_id_))


if __name__ == '__main__':
    multiprocessing.freeze_support()
    RedisMigration.main()
    # ids = RedisMigration.get_ali_redis_instances()
    # for id_ in ids:
    #     RedisMigration.del_ali_instance(id_)
