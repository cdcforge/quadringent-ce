output "instance_name" {
  description = "Nom de l'instance Compute Engine (pour IAP TCP forwarding)."
  value       = google_compute_instance.vm.name
}

output "internal_ip" {
  description = "Adresse IP interne de la VM."
  value       = google_compute_instance.vm.network_interface[0].network_ip
}

output "zone" {
  description = "Zone de la VM."
  value       = google_compute_instance.vm.zone
}
