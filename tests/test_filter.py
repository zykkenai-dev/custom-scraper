"""Tests for core.filter: host deny lists, provider names, niche scope gating."""

from core.filter import (
    is_provider_name,
    is_valid_candidate_lead,
    name_is_denied,
    url_is_denied,
)


class TestUrlDeny:
    def test_deny_host_exact(self):
        assert url_is_denied("https://zillow.com/rental/listings")

    def test_deny_host_subdomain(self):
        assert url_is_denied("https://www.instagram.com/acme/")

    def test_deny_host_suffix(self):
        assert url_is_denied("https://www.agency.gov.uk/contact")

    def test_deny_suffix_edu(self):
        assert url_is_denied("https://some.university.edu/")

    def test_junk_path_fragment(self):
        assert url_is_denied("https://acme.com/blog/how-to-sell")
        assert url_is_denied("https://acme.com/news/latest")

    def test_asset_extension(self):
        assert url_is_denied("https://acme.com/files/price-list.pdf")
        assert url_is_denied("https://acme.com/logo.png")

    def test_legit_business_allowed(self):
        assert not url_is_denied("https://acmerealtypartners.com/")
        assert not url_is_denied("https://acmerealtypartners.com/contact-us")

    def test_portal_siblings(self):
        assert url_is_denied("https://remax.com/agent/smith")
        assert url_is_denied("https://indeed.com/cmp/acme")

    def test_estate_sale_directory_is_denied(self):
        assert url_is_denied("https://www.estatesale.com/companies/WA/Seattle")

    def test_job_boards_and_niche_adjacent_directories_are_denied(self):
        assert url_is_denied("https://builtin.com/job/business-development-manager/1")
        assert url_is_denied("https://www.ziprecruiter.com/Jobs/Real-Estate")
        assert url_is_denied("https://luxurylifestyleawards.com/winners")
        assert url_is_denied("https://business.jeffersoncountywvchamber.org/directory/Details/acme")
        assert url_is_denied("https://code49.com/templates/services.php")
        assert url_is_denied("https://lawyersinarizona.com/real-estate-law")


class TestNameDeny:
    def test_mega_brand(self):
        assert name_is_denied("Google Works")
        assert name_is_denied("Microsoft Dynamics Partner")

    def test_local_business_ok(self):
        assert not name_is_denied("Acme Realty Co.")
        assert not name_is_denied("Smith & Partners")


class TestProviderName:
    def test_provider_marker(self):
        assert is_provider_name("Emailmovers | Email Marketing | Data")

    def test_normal_business_ok(self):
        assert not is_provider_name("Acme Consulting")
        assert not is_provider_name("")

    def test_real_estate_prospector_ok(self):
        assert not is_provider_name("Sunrise Realty Group")


class TestValidCandidate:
    def test_in_niche_accepted(self, real_estate_niche):
        text = "We are a family-owned real estate brokerage helping buyers and sellers."
        assert is_valid_candidate_lead(text, real_estate_niche)

    def test_out_of_scope_rejected(self, real_estate_niche):
        text = "We sell plumbing supplies and fittings."
        assert not is_valid_candidate_lead(text, real_estate_niche)

    def test_corporate_scale_rejected(self, real_estate_niche):
        text = (
            "Our real estate division is a publicly traded company listed on the NYSE "
            "with investor relations pages and quarterly results."
        )
        assert not is_valid_candidate_lead(text, real_estate_niche)

    def test_education_rejected(self, real_estate_niche):
        text = "Our university offers a real estate certificate and faculty of management."
        assert not is_valid_candidate_lead(text, real_estate_niche)

    def test_portal_rejected(self, real_estate_niche):
        text = "Real estate listings — sign in to your account and compare prices."
        assert not is_valid_candidate_lead(text, real_estate_niche)

    def test_provider_rejected(self, real_estate_niche):
        text = "We are a digital marketing agency for real estate agents."
        assert not is_valid_candidate_lead(text, real_estate_niche)

    def test_exclusion_keyword_rejected(self, real_estate_niche):
        text = "Local real estate listings for for sale by owner transactions."
        assert not is_valid_candidate_lead(text, real_estate_niche)

    def test_estate_liquidation_business_rejected(self, real_estate_niche):
        text = "Estate Sales Seattle offers estate liquidation and property services."
        assert not is_valid_candidate_lead(text, real_estate_niche)

    def test_scope_optional_when_require_scope_false(self, real_estate_niche):
        text = "Absolutely no niche keyword here at all."
        assert is_valid_candidate_lead(text, real_estate_niche, require_scope=False)
