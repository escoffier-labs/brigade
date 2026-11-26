"""Package-pinned asset fingerprints for the receipt acknowledgement migration."""

from __future__ import annotations

# Published component-manifest-v1.json fingerprints for offline repair.
MANIFEST_DIGESTS = {
    "v0.30.0+02270bc7ffd92640cd8debf39711eca7c8678fc0": "046dcba26923a792cb49bea266bfad7c0c8d44beb10c2249356faaa346620ba9",
    "v0.32.0+682a0ce07a397e058697c40122d94c4ce02f7db8": "9325fe49f5a37e32d65d8931ef85e682aac5e061a319cdbc1c187509f6f00e59",
}

# From the published component-manifest-v1.json assets. Keep these pins limited to the
# v0.30.0 affected release and the v0.32.0 bundle documented for explicit repair.
_ASSET_PINS: dict[str, dict[tuple[str, str], tuple[int, str]]] = {
    "v0.30.0+02270bc7ffd92640cd8debf39711eca7c8678fc0": {
        ("agent-notify", "darwin-amd64"): (7446896, "f377fc34b417cafaef47ae1588174de4b458ea1ad708a69ef9c371de197d2c51"),
        ("agent-notify", "darwin-arm64"): (6877250, "37ae8797c743a408367fb255b8d1be64729d804c20f2c3cd6c851dec1d85fb1e"),
        ("agent-notify", "linux-amd64"): (7282848, "c4546ed34f6efceb757a5bc2c824d049d6051f9d7e396c9f04e1f7223da8761f"),
        ("agent-notify", "linux-arm64"): (6684832, "ef629f09e55dd4e997f2e7b6f465db4f76ddf6f1a665c87ed3ef3a76d1553e34"),
        ("agent-notify", "windows-amd64"): (
            7493632,
            "5736a152a99110de1ca819c6107715ef560dc8e49b6ba0e7e2ce187d3951d129",
        ),
        ("brigade-code-graph", "darwin-amd64"): (
            12214400,
            "ef891870f5346b5d40662b72ef40aba41847cd3f7d33a25f2f3a75ce00bcc4f6",
        ),
        ("brigade-code-graph", "darwin-arm64"): (
            11894464,
            "e0126f784c8d8e914705798533938f124780fb57c2213d9ad3a1df8457ff18d0",
        ),
        ("brigade-code-graph", "linux-amd64"): (
            13412256,
            "fe9f6511e4bdcb84d6727fe116a3f867df011128abb6a8455bf0317092eab2c3",
        ),
        ("brigade-code-graph", "linux-arm64"): (
            12990096,
            "be1af0a612c0b1cf244ce912cd4966f68865c9f890ccd4e6084e415895a0df0e",
        ),
        ("brigade-code-graph", "windows-amd64"): (
            11239424,
            "1203c89b9d83fc9b7edf340df12105facd0436d374353bd36549c024ef118c02",
        ),
        ("brigade-code-graph-mcp", "darwin-amd64"): (
            11384672,
            "157023e8df7c82225d24b2304549909b2fe76559bb698126873376a41e8e7963",
        ),
        ("brigade-code-graph-mcp", "darwin-arm64"): (
            11129760,
            "745c773784433eef17f56b1ea13333d7ff6ccab9dd0ced4e2db7eec832f7f6d1",
        ),
        ("brigade-code-graph-mcp", "linux-amd64"): (
            12482984,
            "215a01f5b40032d40fc8e4eb7a79bbd57366e29c6a9033a279e4f629ec2e252b",
        ),
        ("brigade-code-graph-mcp", "linux-arm64"): (
            12069632,
            "c40bafe5ef7d49135d0df20d8cb198f4c0465d335b48309bc4ca5fe78acebed1",
        ),
        ("brigade-code-graph-mcp", "windows-amd64"): (
            10401280,
            "fa838e86a30ea253c039a7fda0391bc3d91a76d2d6ebcf4747fa494e73fae8bf",
        ),
        ("brigade-evidence", "darwin-amd64"): (
            19162400,
            "acbac5ac32b4de8d1277a99967aabbce82be79c4fa1b1ad8b506c366ae6ef58d",
        ),
        ("brigade-evidence", "darwin-arm64"): (
            18201714,
            "754a6a3372d536b949da8538b03257e0c95a991443907d5aa030b778f05eabc0",
        ),
        ("brigade-evidence", "linux-amd64"): (
            19161907,
            "9f4519ee192222cf9d16b1b66024410429ca92034a2002b8f0ce4dfd4a54f0b8",
        ),
        ("brigade-evidence", "linux-arm64"): (
            18078157,
            "685f7b88895b16d96f9a34ce783d30eee2f5c7b1012e6e4b4d3f92e6f3b1310d",
        ),
        ("brigade-evidence", "windows-amd64"): (
            19227136,
            "b5702065c75140be2dfb2f8eb3063e314fe5db61b02ab9f8c57fa984ea4789d6",
        ),
        ("sessionfind", "darwin-amd64"): (19166544, "3af563d238139123b6044cc1a66d58ba7909db1daea04f13ac6c67e91891a973"),
        ("sessionfind", "darwin-arm64"): (18201762, "fbcf030d9834b8b514d96d46c8b474c98713a57a29c2cb8b10a871ca837ab3d9"),
        ("sessionfind", "linux-amd64"): (19164686, "4c4b46e64db876a18f38a9674ec53fa63bc1efbd4528d9ba9921e58beefeb141"),
        ("sessionfind", "linux-arm64"): (18082712, "a21b8cd1c1677f614b7cb508e278bacd0d536823261f1d400dbfffe698809c1b"),
        ("sessionfind", "windows-amd64"): (
            19229184,
            "a7fc5a1196774542c04229b990a230656dd0be16808d6bc4a26a3314ae65f784",
        ),
    },
    "v0.32.0+682a0ce07a397e058697c40122d94c4ce02f7db8": {
        ("agent-notify", "darwin-amd64"): (7450992, "a67a2a427760fb1f48736f1cb55e976b2ce52f05962a09beee09072ae26dbdde"),
        ("agent-notify", "darwin-arm64"): (6877250, "5e29edaceee3f003bbacc5796e5a3053aa0e33f684f5907ab1ba82cc4f9ae4a4"),
        ("agent-notify", "linux-amd64"): (7282848, "e6f7f6dd0310230d2ac853ceb76b8eeb21471af7e1da86e9795c8bfefdc1fe8c"),
        ("agent-notify", "linux-arm64"): (6684832, "dc6ba6c3df5b9b3b7ea3995db27f7871d6a68b2433bb16588672fc35db3ec8fb"),
        ("agent-notify", "windows-amd64"): (
            7496192,
            "a12bc62d1184d3005279bed20e6858280ca003cdf31c05dc31078183f804401b",
        ),
        ("brigade-code-graph", "darwin-amd64"): (
            12268544,
            "48e350170a8fa802107b8408d981dd130495a27f13d6d0b3e89e4631ee65ad51",
        ),
        ("brigade-code-graph", "darwin-arm64"): (
            11965648,
            "9180fdfff94ede0254a4650f14f8de1a865786a79c5b5c81c5ec894a3ad90f2a",
        ),
        ("brigade-code-graph", "linux-amd64"): (
            13742736,
            "df0a56fc81314cab5fbbe47d2fcc603ed20b261b8258e21d9ad74d9ac4dfc41e",
        ),
        ("brigade-code-graph", "linux-arm64"): (
            13013640,
            "f991c0af580d39607af8f810e935d5d3697074ee4b9c9af278a36a60bf3ab2ee",
        ),
        ("brigade-code-graph", "windows-amd64"): (
            11297792,
            "f0225d4828ee9daae5e4e35202291f5aecb8183df9823473f5fbfa82b29bd84a",
        ),
        ("brigade-code-graph-mcp", "darwin-amd64"): (
            11449936,
            "a4083e0831266172d283b4d55cb84b236e69ac83e09692ee8c9f26100600aaa7",
        ),
        ("brigade-code-graph-mcp", "darwin-arm64"): (
            11205744,
            "530cbc0f4d625dc9fab2ec57780a60daff17a8537a1d65300fba6998d9635c4f",
        ),
        ("brigade-code-graph-mcp", "linux-amd64"): (
            12818312,
            "cdac322d6f53bbaa9fb5a9c6d2794fe7d218e64f1cd8b3ad7d0c9d254788c239",
        ),
        ("brigade-code-graph-mcp", "linux-arm64"): (
            12161968,
            "4f44d1ff2016ddad60d3641d774a1526d310e734ccdf946c5b1262b28a81f8e7",
        ),
        ("brigade-code-graph-mcp", "windows-amd64"): (
            10462720,
            "36b478b286ec5870b7bda77201eb1fee1f8755e32696636733858e2e436a1206",
        ),
        ("brigade-evidence", "darwin-amd64"): (
            19811136,
            "eb2776d9984f753349c17ad40021d1fb583f5f02439d2c345acd906c591d5cc1",
        ),
        ("brigade-evidence", "darwin-arm64"): (
            18679746,
            "f8e05290af689bac1f092b0b88f71821a7a6aa2d96943292818bd785cbbd2846",
        ),
        ("brigade-evidence", "linux-amd64"): (
            19772149,
            "27b0c3fe8933e76a34a87de92788b36488aa54d900fe44268ca3e97996e73297",
        ),
        ("brigade-evidence", "linux-arm64"): (
            18560919,
            "b6af2103c35fb27e7ed462057d65bd9c1e893e786fca497e0d2f8b6056bfcae7",
        ),
        ("brigade-evidence", "windows-amd64"): (
            19868160,
            "9054e5849dee4c5a97ba28ef1fab9a0b753c555f34b390db65aefa24ae537b63",
        ),
        ("brigade-hub", "linux-amd64"): (17633024, "97dce7bacf2f0f497ed3a90d22beb774ff4cbd781f7791128bd6e14963f1a1c9"),
        ("brigade-hub", "linux-arm64"): (16538568, "c4671417702ebf69858f2004ec138ecef52d29eb32fa0b500bdcd49ef5df37c0"),
        ("sessionfind", "darwin-amd64"): (19815072, "1c90aef02d5d3ad15bfd03038556d6f8a8bc635f216cab4aba047b8ef0d4523b"),
        ("sessionfind", "darwin-arm64"): (18679586, "53ec19538f2589626f3070b7168c00b7bfcc6190f4531e3a3a92ee127e86cdbd"),
        ("sessionfind", "linux-amd64"): (19768809, "ffd6b8e817e50a75111f2da5872323f84668f425cbb568a6972d54cf21473a5d"),
        ("sessionfind", "linux-arm64"): (18562467, "0e1b5f545a78e64aa0a8cedd3a88ac428464fc29f1e2095f851f02dfb82fe993"),
        ("sessionfind", "windows-amd64"): (
            19869696,
            "acbe03400588a9eabc25ecacc7f19566288e2011410da75c97bf7e3f9a7bec7e",
        ),
    },
}


def release_asset_pin(revision: str, component_id: str, platform: str) -> tuple[int, str] | None:
    """Read a fingerprint anchored in the installed CLI package."""
    return _ASSET_PINS.get(revision, {}).get((component_id, platform))
