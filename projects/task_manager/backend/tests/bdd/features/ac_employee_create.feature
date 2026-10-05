# ac.employee.create  (requirements: requirement.employee_directory; operations: operation.employee.create)
Feature: Create Employee succeeds

  Scenario: Create Employee succeeds
    Given the user is signed in as Manager
    When Fill in the Employee form
    And Create Employee
    Then The Employee is created and appears in the Employee list
