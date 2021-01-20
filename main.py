"""阿里☁️ Redis 往 AWS 的离线迁移脚本

通过 RDB 备份文件进行迁移。
先在阿里☁️形成手工备份，下载该备份文件并上传到S3，基于这个备份文件生成新的 Redis 实例。
目前仅支持主从配置。

!pip install redis
!pip install certifi
!pip install aliyun-python-sdk-r-kvstore
"""
import json
from datetime import datetime, timedelta
from time import sleep

import boto3
import certifi
import redis
import requests
from aliyunsdkcore.client import AcsClient
from aliyunsdkr_kvstore.request.v20150101.AllocateInstancePublicConnectionRequest import \
    AllocateInstancePublicConnectionRequest
from aliyunsdkr_kvstore.request.v20150101.CreateBackupRequest import CreateBackupRequest
from aliyunsdkr_kvstore.request.v20150101.DescribeBackupTasksRequest import DescribeBackupTasksRequest
from aliyunsdkr_kvstore.request.v20150101.DescribeBackupsRequest import DescribeBackupsRequest
from aliyunsdkr_kvstore.request.v20150101.DescribeInstanceAttributeRequest import DescribeInstanceAttributeRequest
from aliyunsdkr_kvstore.request.v20150101.DescribeInstancesRequest import DescribeInstancesRequest
from aliyunsdkr_kvstore.request.v20150101.ModifySecurityIpsRequest import ModifySecurityIpsRequest

REGION = 'ap-northeast-1'
PASSWORD = 'AliyunMigration#passw0rd'
MY_IP = ''
BUCKET_NAME = ''

sess = boto3.Session(region_name=REGION)

""" 阿里云 Accesskey_Id 和 Accesskey_secret 请先保存在 AWS Systems Manager 的 Parameter Store 中
名称为:
    ali_ak_pair
值格式为:
    {"accesskey_id":"", "accesskey_secret":""}
权限：
    确认拥有该Key的用户具有上面 import 里列出的 Redis 权限，去掉 Request 尾缀就是操作名称
"""
ssm = sess.client('ssm')
response = ssm.get_parameter(
    Name='ali_ak_pair',
    WithDecryption=True
)
ak_pair = json.loads(response.get('Parameter').get('Value'))
ak = ak_pair.get('accesskey_id')
sk = ak_pair.get('accesskey_secret')

# 取得阿里云当前区域所有 Redis 实例 ID
ali_redis_client = AcsClient(ak, sk, REGION)

request = DescribeInstancesRequest()
request.set_accept_format('json')

ali_instances = json.loads(ali_redis_client.do_action_with_exception(request))
ali_instance_ids = [i.get('InstanceId') for i in ali_instances.get('Instances').get('KVStoreInstance')]

# 这里仅取一个实例为例
ali_instance_id = ali_instance_ids.pop()

# 查看实例详情
request = DescribeInstanceAttributeRequest()
request.set_accept_format('json')

request.set_InstanceId(ali_instance_id)

response = ali_redis_client.do_action_with_exception(request)
json.loads(response.decode())

# 不查看对比 Redis 内容的话，不需运行
# 生成公网地址，域名前缀是 aws-migration- + 实例ID
# 阿里☁️公网地址会生成额外的 Connection 属性对，通过判断数量来决定是否已有公网地址
if len(json.loads(response.decode()).get('Instances').get('DBInstanceAttribute')) < 2:
    port = json.loads(response.decode()).get('Instances').get('DBInstanceAttribute')[0].get('Port')
    request = AllocateInstancePublicConnectionRequest()
    request.set_accept_format('json')

    request.set_ConnectionStringPrefix('aws-migration-' + ali_instance_id)
    request.set_Port(port)
    request.set_InstanceId(ali_instance_id)

    response = ali_redis_client.do_action_with_exception(request)

# 不查看对比 Redis 内容的话，不需运行
# 等待实例状态正常后修改白名单，添加本地到 aws 白名单组
status = 'Unknown'
while status != 'Normal':
    request = DescribeInstanceAttributeRequest()
    request.set_accept_format('json')

    request.set_InstanceId(ali_instance_id)

    response = ali_redis_client.do_action_with_exception(request)
    status = json.loads(response.decode()).get('Instances').get('DBInstanceAttribute')[0].get('InstanceStatus')
    sleep(3)

request = ModifySecurityIpsRequest()
request.set_accept_format('json')

request.set_SecurityIps(MY_IP)
request.set_InstanceId(ali_instance_id)
request.set_SecurityIpGroupName("aws")
request.set_ModifyMode("append")

response = ali_redis_client.do_action_with_exception(request)

# 不查看对比 Redis 内容的话，不需运行
# 查看 Redis 内容
request = DescribeInstanceAttributeRequest()
request.set_accept_format('json')

request.set_InstanceId(ali_instance_id)

response = ali_redis_client.do_action_with_exception(request)

ali_url = json.loads(response.decode()).get('Instances').get('DBInstanceAttribute')[1].get('ConnectionDomain')
port = json.loads(response.decode()).get('Instances').get('DBInstanceAttribute')[1].get('Port')
ali_redis = redis.Redis(host=ali_url, port=port, db=0, password=PASSWORD)
ali_redis.scan()

# 仅用于测试
# 用 S3 中的 CSV 文件填充 Redis，文件小于 500MB，总容量小于 3.5GB
s3 = sess.resource('s3')
bucket = s3.Bucket(BUCKET_NAME)
total_size = 0
for obj in bucket.objects.all():
    if obj.key.startswith('trip data') \
            and obj.key.endswith('csv') \
            and obj.size < 500 * 1024 * 1024 \
            and obj.size + total_size < 3.5 * 1024 * 1024 * 1024:
        response = obj.get()
        ali_redis.set(obj.key, response.get('Body').read())
        total_size += obj.size
print(total_size)
ali_redis.scan()
# r.flushall()

# 创建手工备份
request = CreateBackupRequest()
request.set_accept_format('json')

request.set_InstanceId(ali_instance_id)

response = ali_redis_client.do_action_with_exception(request)

backup_job_id = json.loads(response.decode()).get('BackupJobID')

# 等待备份完成, 不会有文档所述 Finished 的状态
status = 'NoStart'
while status in ['NoStart', 'Checking', 'Preparing', 'Uploading']:
    request = DescribeBackupTasksRequest()
    request.set_accept_format('json')

    request.set_InstanceId(ali_instance_id)
    request.set_BackupJobId(backup_job_id)

    response = ali_redis_client.do_action_with_exception(request)
    status = json.loads(response).get('BackupJobs')[0].get('BackupProgressStatus')
    sleep(10)

# 得到下载地址，排列在前的为最近的备份，假设备份能在 30 分钟内完成，否则调节 startTime
# 得到下载地址，排列在前的为最近的备份，假设备份能在 30 分钟内完成，否则调节 startTime
request = DescribeBackupsRequest()
request.set_accept_format('json')
request.set_StartTime((datetime.now() - timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%MZ'))
request.set_EndTime(datetime.now().strftime('%Y-%m-%dT%H:%MZ'))
request.set_InstanceId(ali_instance_id)

response = ali_redis_client.do_action_with_exception(request)
backup_download_urls = [i.get('BackupDownloadURL') for i in json.loads(response).get('Backups').get('Backup') if
                        i.get('BackupMode') == 'Manual']
while len(backup_download_urls) == 0:
    sleep(3)
    response = ali_redis_client.do_action_with_exception(request)
    backup_download_urls = [i.get('BackupDownloadURL') for i in json.loads(response).get('Backups').get('Backup') if
                            i.get('BackupMode') == 'Manual']
backup_download_url = backup_download_urls[0]
file_name = backup_download_url.split('/')[-1].split('?')[0]

# 下载备份文件备上传到 S3
response = requests.get(backup_download_url)
start = datetime.now()
s3.meta.client.put_object(Bucket=bucket.name, Key=file_name, Body=response.content)
print(s3.ObjectSummary(bucket.name, file_name).size)

# 备份成功上传后，就可以删除阿里☁️ Redis 实例了，即使是手动备份，实例删除后也不存在，这与 AWS 不同
# request = DeleteInstanceRequest()
# request.set_accept_format('json')
#
# request.set_InstanceId(ali_instance_id)
#
# response = ali_redis_client.do_action_with_exception(request)

# 设置在 S3 中 RDB 文件的 ACL，允许 ElastiCache 服务访问，不适合香港等 2019年3月20日之后上线的区域
object_acl = s3.ObjectAcl(bucket.name, file_name)
response = object_acl.put(
    GrantRead='id=540804c33a284a299d2547575ce1010f2312ef3da9b3a053c8bc45bf233e4353',
    GrantReadACP='id=540804c33a284a299d2547575ce1010f2312ef3da9b3a053c8bc45bf233e4353',
)

# 创建新的 AWS Redis 实例, 需要已经建立的子网组, 安全组, 参数组
elasticache = sess.client('elasticache')
CLUSTER_NAME = ali_instance_id
response = elasticache.create_replication_group(
    ReplicationGroupId=CLUSTER_NAME,
    ReplicationGroupDescription='Migration from Aliyun',
    NumNodeGroups=1,
    ReplicasPerNodeGroup=1,
    NodeGroupConfiguration=[
        {
            'NodeGroupId': '0001',
        },
    ],
    CacheNodeType='cache.r5.large',
    Engine='redis',
    EngineVersion='4.0.10',
    CacheParameterGroupName='default.redis4.0',
    CacheSubnetGroupName='sg-pri',
    SecurityGroupIds=[
        'sg-0f3bd26533c0af72d',
    ],
    SnapshotArns=[
        'arn:aws:s3:::' + bucket.name + '/' + file_name,
    ],
    AuthToken=PASSWORD,
    TransitEncryptionEnabled=True,
    AtRestEncryptionEnabled=True,
)
response.get('ReplicationGroup')

# 等待创建完成
status = 'unknown'
while status != 'available':
    response = elasticache.describe_replication_groups(
        ReplicationGroupId=CLUSTER_NAME,
    )
    status = response.get('ReplicationGroups')[0].get('Status')
    sleep(10)

# 检查实例内容
endpoint = response.get('ReplicationGroups')[0].get('NodeGroups')[0].get('PrimaryEndpoint').get('Address')
port = response.get('ReplicationGroups')[0].get('NodeGroups')[0].get('PrimaryEndpoint').get('Port')
aws_redis = redis.Redis(host=endpoint, port=port, db=0, ssl=True, ssl_ca_certs=certifi.where(), password=PASSWORD)
# aws_redis = redis.Redis(host=endpoint, port=port, db=0)
aws_redis.scan()

# 删除 AWS Redis 实例
# response = elasticache.delete_replication_group(
#     ReplicationGroupId=CLUSTER_NAME,
# )
# response.get('ReplicationGroup')
