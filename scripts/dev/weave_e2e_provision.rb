# frozen_string_literal: true
#
# Provisions a local Weave with everything the live Krater<->Weave e2e check
# needs: four users covering member/reviewer/admin/non-member, a slack_id on
# the reviewer, a confidential OAuth application for Krater, and a service key
# with directory:read. Idempotent (find_or_create_by! / upsert-by-email), so
# it can be re-run against the same dev database.
#
# This file lives in the Krater repo (it's Krater's e2e fixture, not a Weave
# behavior change) but runs inside a Weave checkout:
#
#   cd /path/to/weave
#   bundle exec rails runner /path/to/krater/scripts/dev/weave_e2e_provision.rb
#
# See scripts/dev/weave_e2e_setup.py for a wrapper that also writes the
# KRATER_WEAVE_* env vars this produces into a fixture file, and
# docs/dev/weave-e2e.md for the full workflow.
#
# Prints one JSON object to stdout with everything the Python side needs
# (client_id/secret, service API key, user emails/subs). Nothing else should
# be printed to stdout -- logs go to stderr.

require "json"

def log(msg) = warn("[weave_e2e_provision] #{msg}")

GROUPS = {
  "ganymede:member" => "May submit Project Ganymede proposals in Krater",
  "ganymede:reviewer" => "May review Project Ganymede proposals and completion requests",
  "ganymede:admin" => "May override Project Ganymede reviews and budgets"
}.freeze

GROUPS.each { |name, description| Group.find_or_create_by!(name: name) { |g| g.description = description } }
group = GROUPS.keys.index_with { |name| Group.find_by!(name: name) }

def upsert_user(email:, first_name:, last_name:, group_names:, groups:, slack_id: nil)
  user = User.find_or_initialize_by(email: email)
  user.first_name = first_name
  user.last_name = last_name
  user.slack_id = slack_id if slack_id
  if user.new_record?
    user.password = User.generate_secure_password
    user.email_confirmed_at = Time.current
  end
  user.save!
  granter = User.superadmin.first || user
  user.replace_groups!(group_names.map { |n| groups.fetch(n) }, granted_by: granter)
  user
end

member = upsert_user(
  email: "e2e-member@ganymede.test", first_name: "Mira", last_name: "Member",
  group_names: ["ganymede:member"], groups: group
)
reviewer = upsert_user(
  email: "e2e-reviewer@ganymede.test", first_name: "Rae", last_name: "Reviewer",
  group_names: ["ganymede:member", "ganymede:reviewer"], slack_id: "U0E2EREVIEWER",
  groups: group
)
admin = upsert_user(
  email: "e2e-admin@ganymede.test", first_name: "Avi", last_name: "Admin",
  group_names: ["ganymede:member", "ganymede:admin"], groups: group
)
non_member = upsert_user(
  email: "e2e-nonmember@ganymede.test", first_name: "Nia", last_name: "Nonmember",
  group_names: [], groups: group
)
log "users ready: #{[member, reviewer, admin, non_member].map(&:email).join(', ')}"

# -- OAuth application for Krater --------------------------------------------------------------

redirect_uri = ENV.fetch("KRATER_REDIRECT_URI", "http://localhost:8201/auth/callback")
app_name = "Krater (e2e)"
# Secrets are hashed at rest (hash_application_secrets), so the plaintext is
# only ever available on the record returned by .create!. Recreate each run
# rather than updating, so this script always knows the current secret.
Doorkeeper::Application.where(name: app_name).destroy_all
app = Doorkeeper::Application.create!(
  name: app_name,
  redirect_uri: redirect_uri,
  confidential: true,
  scopes: "openid profile email groups slack"
)
log "oauth application ready: uid=#{app.uid}"

# -- Service + service key for the directory API -----------------------------------------------

creator = User.superadmin.first || admin
service = Service.find_or_create_by!(name: "Krater (e2e)") { |s| s.created_by = creator; s.status = "active" }

# Service keys can't be re-read once created (only the digest is stored), so
# revoke any previous e2e keys and mint a fresh one every run.
service.keys.where(name: "krater-e2e").usable.each(&:revoke!)
plaintext_key = Service::Key.generate_api_key
service_key = service.keys.create!(
  name: "krater-e2e",
  scopes: ["directory:read"],
  created_by: creator,
  api_key: plaintext_key
)
log "service key ready: id=#{service_key.id}"

result = {
  issuer: (ENV["OIDC_ISSUER"].presence || "http://localhost:3000"),
  oauth_client_id: app.uid,
  oauth_client_secret: app.plaintext_secret || app.secret,
  service_key: plaintext_key,
  redirect_uri: redirect_uri,
  users: {
    member: { email: member.email, sub: member.p_id },
    reviewer: { email: reviewer.email, sub: reviewer.p_id, slack_id: reviewer.slack_id },
    admin: { email: admin.email, sub: admin.p_id },
    non_member: { email: non_member.email, sub: non_member.p_id }
  }
}

puts JSON.generate(result)
