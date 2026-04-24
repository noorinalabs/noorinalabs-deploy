# Terraform `moved` blocks — state-preserving renames from the pre-#83
# Cloudflare module. Without these, the first `terraform apply` after #83
# merges would destroy + recreate the three existing records (apex A, www
# CNAME, and the isnad-graph subdomain), causing DNS flicker on production.
#
# `moved` blocks instruct Terraform to treat the rename as a state-move
# rather than a destroy-create. No resource changes at the Cloudflare API
# level.
#
# These blocks can be removed in a follow-up PR after the next apply
# completes cleanly and everyone is on post-#83 state.

moved {
  from = cloudflare_record.root_a
  to   = cloudflare_record.prod_apex_a
}

moved {
  from = cloudflare_record.www
  to   = cloudflare_record.www_cname
}

moved {
  from = cloudflare_record.subdomains
  to   = cloudflare_record.legacy_subdomains
}
