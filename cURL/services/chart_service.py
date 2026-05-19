import matplotlib.pyplot as plt


class ChartService:
  def histogram(self, x: list, y: list) -> None:
    plt.figure(figsize=(10, 5))
    plt.bar(x, y)
    plt.xlabel("x")
    plt.ylabel("y")
    plt.title("Histogram")
    plt.tight_layout()
    plt.show()
