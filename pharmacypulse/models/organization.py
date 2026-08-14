"""Business organization models: orgs pharmacists create when claiming a pharmacy."""
from django.db import models


class PharmacyOrg(models.Model):
    """A pharmacist creates an org when they claim a pharmacy."""
    name = models.TextField()
    owner = models.ForeignKey(
        "User", on_delete=models.CASCADE, db_column="owner_id", related_name="owned_orgs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pharmacy_orgs"


class PharmacyTeamMember(models.Model):
    org = models.ForeignKey(
        "PharmacyOrg", on_delete=models.CASCADE, db_column="org_id",
        related_name="team_members",
    )
    user = models.ForeignKey(
        "User", null=True, blank=True, on_delete=models.SET_NULL, db_column="user_id",
        related_name="team_memberships",
    )
    email = models.TextField()
    role = models.TextField(default="member")
    invite_token = models.TextField(blank=True, null=True)
    invited_by = models.IntegerField(blank=True, null=True)
    invited_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pharmacy_team_members"
        constraints = [
            models.UniqueConstraint(
                fields=["org", "email"], name="uniq_team_member_org_email"
            ),
        ]