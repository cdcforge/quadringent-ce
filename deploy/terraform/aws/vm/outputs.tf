output "instance_id" {
  description = "Identifiant de l'instance EC2 (pour SSM Session Manager : aws ssm start-session --target <instance_id>)."
  value       = aws_instance.vm.id
}

output "private_ip" {
  description = "Adresse IP privée de la VM."
  value       = aws_instance.vm.private_ip
}

output "security_group_id" {
  description = "Identifiant du security group appliqué à la VM."
  value       = aws_security_group.vm.id
}
