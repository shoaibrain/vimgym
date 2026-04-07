class Vimgym < Formula
  include Language::Python::Virtualenv

  desc "AI session memory for developers — local, fast, no cloud"
  homepage "https://vimgym.xyz"
  # NOTE: replace `url` and `sha256` with the values from the published PyPI release
  # before merging into homebrew-core / a private tap.
  url "https://files.pythonhosted.org/packages/source/v/vimgym/vimgym-0.1.0.tar.gz"
  sha256 "b43f4761eaca719cbe6b20ba0cff104664194717d3a215037dd9f6262a4cfda4"
  license "MIT"
  head "https://github.com/shoaibrain/vimgym.git", branch: "main"

  depends_on "python@3.12"

  # Python dependencies — generate this list with `brew update-python-resources vimgym`
  # after publishing 0.1.0 to PyPI. The placeholders below let `brew install --HEAD`
  # work via the head url; for the stable formula `update-python-resources` will
  # populate every transitive resource block.
  resource "fastapi" do
    url "https://files.pythonhosted.org/packages/source/f/fastapi/fastapi-0.110.0.tar.gz"
    sha256 "REPLACE_WITH_PYPI_HASH"
  end

  resource "uvicorn" do
    url "https://files.pythonhosted.org/packages/source/u/uvicorn/uvicorn-0.29.0.tar.gz"
    sha256 "REPLACE_WITH_PYPI_HASH"
  end

  resource "watchdog" do
    url "https://files.pythonhosted.org/packages/source/w/watchdog/watchdog-4.0.0.tar.gz"
    sha256 "REPLACE_WITH_PYPI_HASH"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/source/h/httpx/httpx-0.27.0.tar.gz"
    sha256 "REPLACE_WITH_PYPI_HASH"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-13.7.1.tar.gz"
    sha256 "REPLACE_WITH_PYPI_HASH"
  end

  def install
    virtualenv_install_with_resources
  end

  def post_install
    (var/"vimgym").mkpath
    (var/"log").mkpath
  end

  service do
    run [opt_bin/"vg", "start"]
    keep_alive true
    log_path var/"log/vimgym.log"
    error_log_path var/"log/vimgym.log"
    environment_variables VIMGYM_PATH: var/"vimgym"
  end

  test do
    assert_match "vimgym 0.1.0", shell_output("#{bin}/vg --version")
    # `vg config` should not crash on a fresh install (no daemon, no vault yet).
    output = shell_output("VIMGYM_PATH=#{testpath}/vault #{bin}/vg config 2>&1")
    assert_match "vault", output
  end
end
