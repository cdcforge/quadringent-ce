# Module aws/vm — VM EC2 avec k3s (mode par défaut, --target vm). Aucune
# ingress publique par défaut : l'UI Quadringent se consulte par
# SSM port-forward (aws ssm start-session --document-name
# AWS-StartPortForwardingSession), documenté dans le README de ce module.

locals {
  tags = merge(
    {
      purpose          = "quadringent"
      quadringent-site = var.name
    },
    var.tags,
  )

  sg_name = "${var.name}-quadringent-vm"
}

data "aws_ssm_parameter" "al2023" {
  # Le filtre AMI générique sélectionnait parfois l'image Minimal, qui ne
  # fournit pas l'agent SSM nécessaire au tunnel d'administration de la VM.
  # Ce paramètre public AWS désigne l'image AL2023 standard avec noyau 6.1.
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-${var.architecture}"
}

data "cloudinit_config" "this" {
  gzip          = false
  base64_encode = false

  part {
    content_type = "text/cloud-config"
    content = templatefile("${path.module}/cloud-init.tpl.yaml", {
      k3s_channel = var.k3s_channel
      site_name   = var.name
    })
  }
}

resource "aws_security_group" "vm" {
  name        = local.sg_name
  description = "Quadringent VM (site ${var.name}) : aucune ingress publique par defaut."
  vpc_id      = var.vpc_id
  tags        = local.tags

  egress {
    description = "Sortie complete (registre images, Snowflake, IBM i, SSM)."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  count             = length(var.allowed_ssh_cidrs)
  security_group_id = aws_security_group.vm.id
  description       = "Acces SSH restreint (optionnel, a defaut utiliser SSM Session Manager)."
  cidr_ipv4         = var.allowed_ssh_cidrs[count.index]
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "ui" {
  count             = length(var.allowed_ui_cidrs)
  security_group_id = aws_security_group.vm.id
  description       = "Acces direct restreint a UI Quadringent (defaut recommande : SSM port-forward, ne pas renseigner)."
  cidr_ipv4         = var.allowed_ui_cidrs[count.index]
  from_port         = var.ui_port
  to_port           = var.ui_port
  ip_protocol       = "tcp"
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = var.instance_role_name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_instance" "vm" {
  depends_on                  = [aws_iam_role_policy_attachment.ssm]
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.vm.id]
  iam_instance_profile        = var.instance_profile_name
  key_name                    = var.ssh_key_name != "" ? var.ssh_key_name : null
  user_data                   = data.cloudinit_config.this.rendered
  user_data_replace_on_change = true

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_size = var.root_volume_size_gb
    encrypted   = true
  }

  tags = merge(local.tags, { Name = "${var.name}-quadringent-vm" })
}
