# Module gcp/vm — VM Compute Engine avec k3s (mode par défaut, --target vm).
# Aucune IP publique par défaut : l'accès recommandé passe par SSH sur IAP
# avec un transfert local vers l'API k3s, documenté dans le README.

locals {
  labels = merge(
    {
      purpose          = "quadringent"
      quadringent-site = var.name
    },
    var.labels,
  )

  fw_name = "${var.name}-quadringent-vm-ssh-iap"
  vm_tag  = "${var.name}-quadringent-vm"
}

data "google_compute_image" "cos" {
  family  = "ubuntu-2404-lts-amd64"
  project = "ubuntu-os-cloud"
}

resource "google_compute_firewall" "ssh_iap" {
  name    = local.fw_name
  project = var.project_id
  network = var.network

  # 35.235.240.0/20 est la plage fixe d'IAP TCP forwarding : c'est le seul
  # accès SSH autorisé par défaut, sans exposer la VM publiquement.
  source_ranges = concat(["35.235.240.0/20"], var.allowed_ssh_ranges)
  target_tags   = [local.vm_tag]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_instance" "vm" {
  project      = var.project_id
  name         = "${var.name}-quadringent-vm"
  machine_type = var.machine_type
  zone         = var.zone
  labels       = local.labels
  tags         = [local.vm_tag]

  boot_disk {
    initialize_params {
      image = data.google_compute_image.cos.self_link
      size  = var.boot_disk_size_gb
      type  = "pd-ssd"
    }
  }

  network_interface {
    network    = var.network
    subnetwork = var.subnetwork
    # Le fournisseur n'a pas nécessairement de projet par défaut quand
    # project est défini sur la ressource ; un nom court de sous-réseau
    # nécessite alors son projet explicite lors de l'apply.
    subnetwork_project = var.project_id

    dynamic "access_config" {
      for_each = var.assign_public_ip ? [1] : []
      content {}
    }
  }

  service_account {
    email  = var.service_account_email
    scopes = ["cloud-platform"]
  }

  metadata = {
    startup-script = templatefile("${path.module}/startup-script.tpl.sh", {
      k3s_channel = var.k3s_channel
      site_name   = var.name
    })
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
}
