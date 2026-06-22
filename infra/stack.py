import aws_cdk as cdk
from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    Duration,
    aws_opensearchservice as opensearch,
    aws_iam as iam,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_targets as targets,
)
from constructs import Construct


class ConstructRagStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── OpenSearch ──────────────────────────────────────────────────────
        domain = opensearch.Domain(
            self,
            "Domain",
            domain_name="construction-rag",
            version=opensearch.EngineVersion.OPENSEARCH_2_9,
            capacity=opensearch.CapacityConfig(
                data_node_instance_type="t3.medium.search",
                data_nodes=1,
            ),
            ebs=opensearch.EbsOptions(
                enabled=True,
                volume_size=30,
                volume_type=ec2.EbsDeviceVolumeType.GP3,
            ),
            encryption_at_rest=opensearch.EncryptionAtRestOptions(enabled=True),
            node_to_node_encryption=True,
            enforce_https=True,
            access_policies=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    principals=[iam.AccountRootPrincipal()],
                    actions=["es:*"],
                    resources=["*"],
                )
            ],
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── 前端 EC2 ─────────────────────────────────────────────────────────
        # IAM Role：SSM + Bedrock + OpenSearch
        role = iam.Role(
            self,
            "FrontendRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonBedrockFullAccess"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonS3ReadOnlyAccess"),
            ],
        )
        # OpenSearch 访问
        role.add_to_policy(iam.PolicyStatement(
            actions=["es:*"],
            resources=[domain.domain_arn + "/*"],
        ))

        # 默认 VPC（全栈共用一次 lookup）
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)

        # 安全组：仅开放 Streamlit 端口（8501）和 SSM（无需 22）
        sg = ec2.SecurityGroup(
            self,
            "FrontendSG",
            vpc=vpc,
            description="construction-rag frontend",
            allow_all_outbound=True,
        )
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(8501), "Streamlit")

        # 最新 Amazon Linux 2023 AMI
        ami = ec2.MachineImage.latest_amazon_linux2023()

        instance = ec2.Instance(
            self,
            "Frontend",
            instance_type=ec2.InstanceType("t3.small"),
            machine_image=ami,
            vpc=vpc,
            security_group=sg,
            role=role,
            # 无 UserData：SSH 进去后手动配置一次即可
        )

        # ── ALB ──────────────────────────────────────────────────────────────

        alb_sg = ec2.SecurityGroup(
            self, "AlbSG",
            vpc=vpc,
            description="construction-rag ALB",
            allow_all_outbound=True,
        )
        alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP")

        # ALB 允许访问 EC2 8501
        sg.add_ingress_rule(ec2.Peer.security_group_id(alb_sg.security_group_id),
                            ec2.Port.tcp(8501), "from ALB")

        alb = elbv2.ApplicationLoadBalancer(
            self, "Alb",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
        )

        tg = elbv2.ApplicationTargetGroup(
            self, "TG",
            vpc=vpc,
            port=8501,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[targets.InstanceTarget(instance, port=8501)],
            health_check=elbv2.HealthCheck(
                path="/healthz",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(10),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )

        alb.add_listener(
            "Listener",
            port=80,
            default_target_groups=[tg],
        )

        # ── Outputs ──────────────────────────────────────────────────────────
        CfnOutput(self, "OpenSearchEndpoint", value=domain.domain_endpoint,
                  description="export OPENSEARCH_HOST=<this value>")
        CfnOutput(self, "OpenSearchDomainArn", value=domain.domain_arn)
        CfnOutput(self, "FrontendInstanceId", value=instance.instance_id,
                  description="SSM: aws ssm start-session --target <this value>")
        CfnOutput(self, "AppUrl", value=f"http://{alb.load_balancer_dns_name}",
                  description="Streamlit: http://<alb-dns>")
