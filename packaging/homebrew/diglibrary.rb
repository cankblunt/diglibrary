# The formula for the project's tap, kept here because this is where it is
# reviewed and versioned; the tap repository is a copy of this one file.
#
# **Why a tap at all, when `pipx install diglibrary` is one line.** Because that
# line is the third of three: somebody has to install Homebrew, then use it to
# get Python, ffmpeg and pipx, then run `pipx ensurepath`, then close the
# Terminal and open it again because their shell has not heard about the new
# path. ffmpeg is the reason Homebrew was needed in the first place — it is not
# a Python package and nothing pip does can bring it.
#
# So the tap is not a second way to install the same thing. It is the route that
# removes the two steps most likely to lose somebody:
#
#     brew tap cankblunt/diglibrary
#     brew install diglibrary
#
# `ffmpeg` arrives as a dependency, the command is on the path when the second
# line returns, and there is no reopened window. `chromaprint` stays out on
# purpose: it is genuinely optional (fingerprinting), and a hundred megabytes
# nobody asked for is not a good first impression.
#
# To update for a release: change `url`, `sha256` and `version`, then
#   brew audit --strict --online Formula/diglibrary.rb
#   brew fetch --build-from-source diglibrary
#   brew install --build-from-source diglibrary
# The resources move only when a dependency does.

class Diglibrary < Formula
  include Language::Python::Virtualenv

  desc "Identify albums, detect transcoded FLAC, and rename and tag reversibly"
  homepage "https://cankblunt.github.io/diglibrary/"
  url "https://files.pythonhosted.org/packages/f7/23/461ecf94b5568549b30ea820b45787e8ee20bc1175a3e590211781ede86d/diglibrary-1.6.1.tar.gz"
  sha256 "846d8e28b9593a03270ff1cbca45bc60e54d5b3a8a7a355264cf03c757cb6967"
  license "MIT"

  # macOS only, and the application says so itself on any other system. Declared
  # here as well so `brew install` refuses before it builds anything.
  depends_on "ffmpeg"
  depends_on :macos
  depends_on "numpy"
  # 3.13 rather than the project's declared floor of 3.12, because Homebrew's
  # `numpy` is only built for the interpreters Homebrew currently ships — 3.13
  # and 3.14 — and this formula borrows that build rather than compiling one.
  # It is not a wider promise than the PyPI route already makes: `pipx install
  # diglibrary` uses whichever interpreter `brew install python` brought, which
  # is the same one.
  depends_on "python@3.13"

  resource "bottle" do
    url "https://files.pythonhosted.org/packages/7a/71/cca6167c06d00c81375fd668719df245864076d284f7cb46a694cbeb5454/bottle-0.13.4.tar.gz"
    sha256 "787e78327e12b227938de02248333d788cfe45987edca735f8f88e03472c3f47"
  end

  resource "mutagen" do
    url "https://files.pythonhosted.org/packages/df/70/1675da133ea92227da41bf5b24e1c66be597ff736a1533ade41da986852f/mutagen-1.48.1.tar.gz"
    sha256 "8f95637ab9f6f305cec6bd1294e197debe207998e3e068596563c74f86b0a173"
  end

  resource "proxy-tools" do
    url "https://files.pythonhosted.org/packages/f2/cf/77d3e19b7fabd03895caca7857ef51e4c409e0ca6b37ee6e9f7daa50b642/proxy_tools-0.1.0.tar.gz"
    sha256 "ccb3751f529c047e2d8a58440d86b205303cf0fe8146f784d1cbcd94f0a28010"
  end

  resource "pyobjc-core" do
    url "https://files.pythonhosted.org/packages/a5/78/abc4ce5920305780aeb36b4067a86253378b36e29ba96673a3deb02eb03a/pyobjc_core-12.2.2.tar.gz"
    sha256 "3906452339cd06a3bb07df103c2511d4cb0f7a22d8771c0b802eba15d9a642b6"
  end

  resource "pyobjc-framework-cocoa" do
    url "https://files.pythonhosted.org/packages/75/76/49c6da2c6a831020b4854ba20079d5a1030474bffc776b7b73c2eeff8c15/pyobjc_framework_cocoa-12.2.2.tar.gz"
    sha256 "c96c0ef69a71afbbb0e6a7d594b455c5fe47d62e0db376ee7a2b4b828c16ace9"
  end

  resource "pyobjc-framework-quartz" do
    url "https://files.pythonhosted.org/packages/35/b1/426a37c7ae37280b3ffca2571fb48f211946aee2f4ca31a603ed1943c4a7/pyobjc_framework_quartz-12.2.2.tar.gz"
    sha256 "810f97b210cfd93704d240860286dfd6df09f9f1c52525fc5c2166723aea3f9e"
  end

  resource "pyobjc-framework-security" do
    url "https://files.pythonhosted.org/packages/c2/92/c304b7fc3a0fe7484a2a3cf25711e70c8fa2b6969d82f4010e35b9af2164/pyobjc_framework_security-12.2.2.tar.gz"
    sha256 "33efab1ff7d18570148f8f3ddd44eca305f733aee00b9115d5263bef81018f65"
  end

  resource "pyobjc-framework-uniformtypeidentifiers" do
    url "https://files.pythonhosted.org/packages/70/c6/31ac40c4d918baa36ca06d196bfec0f47f804a74684988cf424060469d98/pyobjc_framework_uniformtypeidentifiers-12.2.2.tar.gz"
    sha256 "12f8ba77dcc949ffb9f0f48743cae326aebec8e69cb1ac55a1d1e04dca7bd59a"
  end

  resource "pyobjc-framework-webkit" do
    url "https://files.pythonhosted.org/packages/6f/1f/766e338197f7051c25f23cb0d350caa88234b31c3a759127f2cbb67f3376/pyobjc_framework_webkit-12.2.2.tar.gz"
    sha256 "e5588df2a73b377b59a994cc2a78b467e4341f4e4d28b52e8671e21a2811d3c1"
  end

  resource "pywebview" do
    url "https://files.pythonhosted.org/packages/59/4a/05307135dafba67778669d194bd1a3822a7685ec9ee8a6d7e70856c1a551/pywebview-6.2.1.tar.gz"
    sha256 "71b7136752e40824655304d938efb62014218d1a90bd8e87e1cbdb1ce9c466af"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/f6/cc/6253133b5bb138fc3306cebfbda2c520f545d36b5be2c7255cc528bb45d6/typing_extensions-4.16.0.tar.gz"
    sha256 "dc983d19a509c94dba722ee6abd33940f7c05a89e243c47e907eb4db6f1a43e5"
  end

  def install
    # numpy is a Homebrew dependency rather than a resource: building it from
    # source inside this virtualenv costs minutes and a working compiler
    # toolchain, for a build Homebrew already keeps. A `.pth` file is what lets
    # the virtualenv see it, and it is written for the one interpreter this
    # formula was built against.
    series = "3.13"
    virtualenv_install_with_resources(using: "python@#{series}")

    site = formula_opt_prefix("numpy")/Language::Python.site_packages("python#{series}")
    # Refused rather than installed. Pointed at a folder Homebrew's numpy does
    # not build for, the formula compiles, installs, passes its own build, and
    # every launch fails on `ModuleNotFoundError: numpy`. A path that is not
    # there is a fact this can check, and the day Homebrew moves numpy off this
    # series it must stop here rather than ship an application that cannot
    # start.
    odie "numpy has no #{series} build at #{site}; this formula's python must match it" \
      unless site.directory?
    (libexec/Language::Python.site_packages("python#{series}")/"homebrew-numpy.pth").write "#{site}\n"
  end

  test do
    # Not a smoke test of the window — this has no display and the application
    # says so. What is asserted is that the entry point is on the path, answers,
    # and reports the version this formula claims to have installed.
    assert_match "Open the DigLibrary window", shell_output("#{bin}/diglibrary --help")
    assert_match "Applications folder", shell_output("#{bin}/diglibrary make-icon --help")
    output = shell_output("#{libexec}/bin/python -c " \
                          "'from diglibrary.metadata.authentication import application_version;" \
                          "print(application_version())'")
    assert_equal version.to_s, output.strip
  end
end
